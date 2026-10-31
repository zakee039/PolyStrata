// src/poly/session_disk.hpp - opt-in single-slot disk KV snapshots for strata-poly.
// One checkpoint, exact token identity, recurrent state, attention rows; no model weights.
// The default GEN-only path does not call any of this code.
#pragma once
#include "conversations.hpp"
#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cinttypes>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

#ifdef _WIN32
#include <io.h>
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <unistd.h>
#endif

namespace strata::poly::disk {

using Clock = std::chrono::steady_clock;
inline double elapsed_ms(Clock::time_point t) {
    return std::chrono::duration<double, std::milli>(Clock::now() - t).count();
}
inline uint64_t hash(const void* ptr, size_t count, uint64_t h = 1469598103934665603ull) {
    const auto* p = static_cast<const uint8_t*>(ptr);
    for (size_t i = 0; i < count; ++i) h = (h ^ p[i]) * 1099511628211ull;
    return h;
}

struct Error : std::runtime_error {
    const char* kind;
    bool fatal;
    explicit Error(const char* k, const std::string& why, bool f = false)
        : std::runtime_error(why), kind(k), fatal(f) {}
};

struct Header {
    char magic[8];
    uint64_t version;
    uint64_t tokens;
    uint64_t row_size;
    uint64_t state_size;
    uint64_t max_context;
    uint64_t model_signature;
    uint64_t header_hash;
};
static_assert(sizeof(Header) == 64, "disk KV header must be fixed-width");

// Use at most 16 MiB per GPU/SSD transfer; progress lines are valid for server.py's watchdog.
constexpr size_t kBlock = 16u << 20;
constexpr uint64_t kMaxState = 2ull << 30;     // avoid massive allocation from malformed inputs
constexpr uint64_t kMaxFile = 32ull << 30;      // the configured max-context bounds it further
constexpr char kMagic[8] = {'P','O','L','Y','K','V','1','!'};

inline uint64_t header_hash(const Header& h) { return hash(&h, 56); }
inline uint64_t payload_size(uint64_t count, uint64_t row, uint64_t state) {
    if (!count || !row || row > (16ull << 20) || state > kMaxState ||
        count > (kMaxFile - sizeof(Header) - sizeof(uint64_t) - state) / row)
        throw Error("invalid", "session geometry is not safe");
    const uint64_t size = sizeof(Header) + count * sizeof(int32_t) + state + count * row + sizeof(uint64_t);
    if (size > kMaxFile) throw Error("invalid", "session exceeds 32 GiB limit");
    return size;
}
inline void progress(uint64_t done, uint64_t total) {
    std::printf("SESSION %" PRIu64 " %" PRIu64 "\n", done, total);
    std::fflush(stdout);
}
inline void wait_note(const char* phase, int seconds = 360) {
    std::printf("SWAIT %s %d\n", phase, seconds);
    std::fflush(stdout);
}
inline void read_exact(std::FILE* f, void* dst, size_t n) {
    if (n && std::fread(dst, 1, n, f) != n) throw Error("invalid", "session file is truncated");
}
inline void write_exact(std::FILE* f, const void* src, size_t n) {
    if (n && std::fwrite(src, 1, n, f) != n) throw Error("io", "session write failed");
}
inline void chunks_write(std::FILE* f, const void* p, size_t bytes, uint64_t& done, uint64_t total, uint64_t& digest) {
    const auto* src = static_cast<const uint8_t*>(p);
    while (bytes) {
        const size_t n = std::min(bytes, kBlock);
        write_exact(f, src, n);
        digest = hash(src, n, digest);
        bytes -= n;
        src += n;
        done += n;
        progress(done, total);
    }
}
inline void chunks_read(std::FILE* f, void* p, size_t bytes, uint64_t& done, uint64_t total, uint64_t& digest) {
    auto* dst = static_cast<uint8_t*>(p);
    while (bytes) {
        const size_t n = std::min(bytes, kBlock);
        read_exact(f, dst, n);
        digest = hash(dst, n, digest);
        bytes -= n;
        dst += n;
        done += n;
        progress(done, total);
    }
}
inline void publish(const std::string& from, const std::string& to) {
#ifdef _WIN32
    if (!MoveFileExA(from.c_str(), to.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH))
        throw Error("io", "session file rename failed");
#else
    if (std::rename(from.c_str(), to.c_str()) != 0) throw Error("io", "session file rename failed");
#endif
}

struct FileCloser { void operator()(std::FILE* f) const { if (f) std::fclose(f); } };

template<class E>
struct SessionFile {
    E& engine;
    Conversations<E>& cache;
    uint64_t signature;

    // Called between requests only (Python's Service.fifo holds the engine protocol).
    void save(const std::string& path) {
        if (path.empty()) throw Error("invalid", "empty session filename");
        const auto start = Clock::now();
        const auto point = cache.disk_point();
        if (point.count < 1) throw Error("invalid", "there is no conversation checkpoint");
        const size_t row = engine.row_bytes(), state_n = engine.state_bytes();
        const uint64_t total = payload_size((uint64_t) point.count, row, state_n);
        if (point.count >= engine.max_context()) throw Error("invalid", "checkpoint outside model context");
        for (int64_t i = 0; i < point.count; ++i)
            if (point.tokens[i] < 0 || point.tokens[i] >= engine.n_vocab())
                throw Error("invalid", "image or out-of-range tokens cannot be saved in V1");
        std::vector<uint8_t> live;
        const uint8_t* state = point.state;
        if (!state) {
            live.resize(state_n);
            engine.save_state(live.data());
            state = live.data();
        }

        Header h{};
        std::memcpy(h.magic, kMagic, 8);
        h.version = 1;
        h.tokens = (uint64_t) point.count;
        h.row_size = row;
        h.state_size = state_n;
        h.max_context = engine.max_context();
        h.model_signature = signature;
        h.header_hash = header_hash(h);

        // File is not published until its content, hash and flush succeed.
        const std::string tmp = path + ".saving-" + std::to_string(
            (uint64_t) Clock::now().time_since_epoch().count());
        std::FILE* f = std::fopen(tmp.c_str(), "wb");
        if (!f) throw Error("io", "cannot create temporary session file");
        try {
            uint64_t done = 0, digest = hash("", 0);
            write_exact(f, &h, sizeof h);
            done += sizeof h;
            progress(done, total);
            chunks_write(f, point.tokens, (size_t) point.count * sizeof(int32_t), done, total, digest);
            chunks_write(f, state, state_n, done, total, digest);
            const size_t chunk_rows = std::max<size_t>(1, kBlock / row);
            std::vector<uint8_t> buf(chunk_rows * row);
            for (int64_t at = 0; at < point.count;) {
                const int64_t n = std::min<int64_t>((int64_t) chunk_rows, point.count - at);
                engine.save_rows(at, n, buf.data());
                chunks_write(f, buf.data(), (size_t) n * row, done, total, digest);
                at += n;
            }
            write_exact(f, &digest, sizeof digest);
            done += sizeof digest;
            progress(done, total);
            wait_note("flush");
            if (std::fflush(f) != 0) throw Error("io", "session flush failed");
#ifdef _WIN32
            if (_commit(_fileno(f)) != 0) throw Error("io", "session disk commit failed");
#else
            if (fsync(fileno(f)) != 0) throw Error("io", "session disk sync failed");
#endif
            if (std::fclose(f) != 0) {
                f = nullptr;
                throw Error("io", "session file close failed");
            }
            f = nullptr;
            wait_note("publish");
            publish(tmp, path);
            std::printf("SAVED %" PRIu64 " %" PRIu64 " %.1f\n", h.tokens, total, elapsed_ms(start));
            std::fflush(stdout);
        } catch (...) {
            if (f) std::fclose(f);
            std::remove(tmp.c_str());
            throw;
        }
    }

    // Validate ALL bytes before any device write. On GPU transfer failure, the caller must terminate the
    // engine: a half-restored cache must never be used to produce another answer.
    void restore(const std::string& path) {
        if (path.empty()) throw Error("invalid", "empty session filename");
        const auto start = Clock::now();
        std::unique_ptr<std::FILE, FileCloser> f(std::fopen(path.c_str(), "rb"));
        if (!f) throw Error("invalid", "the session file does not exist");
        Header h{};
        read_exact(f.get(), &h, sizeof h);
        if (std::memcmp(h.magic, kMagic, 8) || h.version != 1 || h.header_hash != header_hash(h) ||
            h.row_size != engine.row_bytes() || h.state_size != engine.state_bytes() ||
            h.max_context != (uint64_t) engine.max_context() || h.model_signature != signature ||
            h.tokens >= (uint64_t) engine.max_context())
            throw Error("invalid", "session format, model, or engine configuration differs");
        const uint64_t total = payload_size(h.tokens, h.row_size, h.state_size);
        if (std::filesystem::file_size(path) != total)
            throw Error("invalid", "session file size is inconsistent with its header");
        uint64_t done = sizeof h, digest = hash("", 0);
        progress(done, total);
        std::vector<int32_t> tokens((size_t) h.tokens);
        std::vector<uint8_t> state((size_t) h.state_size);
        chunks_read(f.get(), tokens.data(), tokens.size() * sizeof(int32_t), done, total, digest);
        chunks_read(f.get(), state.data(), state.size(), done, total, digest);
        // This is a local file, but no token may exceed the model vocabulary or encode a fake image;
        // multimodal snapshots are deliberately not supported in V1.
        for (int32_t t : tokens)
            if (t < 0 || t >= engine.n_vocab())
                throw Error("invalid", "session contains invalid or image token ids");
        const size_t row = (size_t) h.row_size;
        const size_t chunk_rows = std::max<size_t>(1, kBlock / row);
        std::vector<uint8_t> buf(chunk_rows * row);
        for (int64_t at = 0; at < (int64_t) h.tokens;) {
            const int64_t n = std::min<int64_t>((int64_t) chunk_rows, (int64_t) h.tokens - at);
            chunks_read(f.get(), buf.data(), (size_t) n * row, done, total, digest);
            at += n;
        }
        uint64_t expected = 0;
        read_exact(f.get(), &expected, sizeof expected);
        done += sizeof expected;
        progress(done, total);
        if (digest != expected) throw Error("invalid", "session data checksum mismatch");

        // Verified. Read the rows a second time in bounded blocks for device transfer.
        // Keep the same file descriptor open; never start writing to the device on a truncated file.
        wait_note("transfer");
#ifdef _WIN32
        if (_fseeki64(f.get(), (long long) (sizeof h + tokens.size() * sizeof(int32_t) + state.size()), SEEK_SET) != 0)
#else
        if (fseeko(f.get(), (off_t) (sizeof h + tokens.size() * sizeof(int32_t) + state.size()), SEEK_SET) != 0)
#endif
            throw Error("io", "session seek failed");
        try {
            uint64_t transfer_hash = hash(tokens.data(), tokens.size() * sizeof(int32_t));
            transfer_hash = hash(state.data(), state.size(), transfer_hash);
            engine.reset();
            for (int64_t at = 0; at < (int64_t) h.tokens;) {
                const int64_t n = std::min<int64_t>((int64_t) chunk_rows, (int64_t) h.tokens - at);
                read_exact(f.get(), buf.data(), (size_t) n * row);
                transfer_hash = hash(buf.data(), (size_t) n * row, transfer_hash);
                engine.load_rows(buf.data(), at, n, at, n);
                progress(total, total); // heartbeat during the second pass (the first pass is already 100%)
                at += n;
            }
            if (transfer_hash != expected)
                throw std::runtime_error("session changed during verified restore");
            cache.disk_adopt(std::move(tokens), state.data());
        } catch (const std::exception& e) {
            throw Error("io", std::string("GPU session restore was interrupted: ") + e.what(), true);
        }
        std::printf("RESTORED %" PRIu64 " %" PRIu64 " %.1f\n", h.tokens, total, elapsed_ms(start));
        std::fflush(stdout);
    }
};

} // namespace strata::poly::disk
