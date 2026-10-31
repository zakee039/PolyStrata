// CPU-only regression for the optional strata-poly disk checkpoint protocol.
// g++ -std=c++20 -O2 -Isrc/poly src/poly/session_disk_test.cpp -o session_disk_test && ./session_disk_test
#include "session_disk.hpp"
#include <array>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <iostream>
#include <random>
#include <string>
#include <vector>

using strata::poly::CacheOptions;
using strata::poly::Conversations;
using strata::poly::disk::Error;
using strata::poly::disk::SessionFile;

namespace {
struct Fake {
    static constexpr int ctx = 4096, vocab = 2048;
    uint64_t state = 1;
    int64_t at = 0;
    std::array<uint64_t, ctx> rows{};
    int max_context() const { return ctx; }
    int n_vocab() const { return vocab; }
    size_t row_bytes() const { return sizeof(uint64_t); }
    size_t state_bytes() const { return sizeof(uint64_t); }
    void reset() { state = 1; at = 0; }
    void save_state(uint8_t* p) { std::memcpy(p, &state, 8); }
    void load_state(const uint8_t* p, int64_t pos) { std::memcpy(&state, p, 8); at = pos; }
    void save_rows(int64_t first, int64_t n, uint8_t* out) {
        std::memcpy(out, rows.data() + first, (size_t) n * 8);
    }
    void load_rows(const uint8_t* src, int64_t from, int64_t count, int64_t first, int64_t n) {
        assert(first >= from && first + n <= from + count);
        std::memcpy(rows.data() + first, src + (size_t) (first - from) * 8, (size_t) n * 8);
    }
    void eval(const int32_t* tokens, int n) {
        for (int i = 0; i < n; ++i) {
            state = (state ^ (uint32_t) tokens[i]) * 1099511628211ull;
            rows[(size_t) at++] = state;
        }
    }
};
}  // namespace

int main() {
    auto path = std::filesystem::temp_directory_path() / "poly_session_disk_test.bin";
    std::filesystem::remove(path);
    Fake a;
    CacheOptions opt;
    opt.checkpoints = 6;
    Conversations<Fake> ca(a, opt);
    std::vector<int32_t> prompt;
    for (int i = 0; i < 150; ++i) prompt.push_back(10 + i);
    assert(ca.resume(prompt) == 0);
    for (int i = 0; i < (int) prompt.size(); ++i) {
        ca.before(i, i + 1 == (int) prompt.size());
        a.eval(&prompt[(size_t) i], 1);
        ca.read(&prompt[(size_t) i], 1);
    }
    ca.prompt_was((int64_t) prompt.size());
    for (int i = 0; i < 6; ++i) {
        const int32_t t = 512 + i;
        a.eval(&t, 1);
        ca.read(&t, 1);
        prompt.push_back(t);
    }
    const auto snap = ca.disk_point();
    assert(snap.count == 149);  // before the final prompt token, not the speculative output
    const uint64_t hash_at = a.rows[148];

    SessionFile<Fake>{a, ca, 1234}.save(path.string());
    assert(std::filesystem::file_size(path) > 149 * sizeof(uint64_t));

    Fake b;
    Conversations<Fake> cb(b, opt);
    SessionFile<Fake>{b, cb, 1234}.restore(path.string());
    assert(cb.seq().size() == 149);
    assert(b.at == 149 && b.state == hash_at && b.rows[148] == hash_at);
    assert(cb.resume(prompt) == 149);
    // Continue from the recovered checkpoint, and compare to a cold engine.
    for (int i = 149; i < (int) prompt.size(); ++i) {
        b.eval(&prompt[(size_t)i], 1);
        cb.read(&prompt[(size_t)i], 1);
    }
    assert(b.state == a.state);
    for (int i = 0; i < (int) prompt.size(); ++i) assert(b.rows[i] == a.rows[i]);

    // A mismatched model signature and a corrupt file are refused before device writes.
    bool wrong = false;
    try { SessionFile<Fake>{b, cb, 9999}.restore(path.string()); }
    catch (const Error& e) { wrong = std::strcmp(e.kind, "invalid") == 0; }
    assert(wrong);
    {
        std::FILE* f = std::fopen(path.string().c_str(), "r+b");
        assert(f);
        assert(std::fseek(f, 100, SEEK_SET) == 0);
        std::fputc(123, f);
        std::fclose(f);
    }
    wrong = false;
    try { SessionFile<Fake>{b, cb, 1234}.restore(path.string()); }
    catch (const Error& e) { wrong = std::strcmp(e.kind, "invalid") == 0; }
    assert(wrong);
    assert(b.state == a.state);
    std::filesystem::remove(path);
    std::cout << "poly_session_disk_test: passed\n";
}
