//! Secrets are encrypted with Windows DPAPI, scoped to this Windows user.
//! No API key or Cloudflare token is stored in normal settings or model JSON.
use std::{ffi::c_void, fs, os::windows::ffi::OsStrExt, ptr};

#[repr(C)]
struct DataBlob {
    len: u32,
    data: *mut u8,
}

#[link(name = "Crypt32")]
unsafe extern "system" {
    fn CryptProtectData(
        input: *const DataBlob,
        description: *const u16,
        entropy: *const DataBlob,
        reserved: *mut c_void,
        prompt: *mut c_void,
        flags: u32,
        output: *mut DataBlob,
    ) -> i32;
    fn CryptUnprotectData(
        input: *const DataBlob,
        description: *mut *mut u16,
        entropy: *const DataBlob,
        reserved: *mut c_void,
        prompt: *mut c_void,
        flags: u32,
        output: *mut DataBlob,
    ) -> i32;
}

#[link(name = "Kernel32")]
unsafe extern "system" {
    fn LocalFree(memory: *mut c_void) -> *mut c_void;
}

#[link(name = "Bcrypt")]
unsafe extern "system" {
    fn BCryptGenRandom(algorithm: *mut c_void, buffer: *mut u8, len: u32, flags: u32) -> i32;
}

pub fn encrypt(data: &[u8]) -> Result<Vec<u8>, String> {
    let input = DataBlob {
        len: data.len() as u32,
        data: data.as_ptr() as *mut u8,
    };
    let mut output = DataBlob {
        len: 0,
        data: ptr::null_mut(),
    };
    // CRYPTPROTECT_UI_FORBIDDEN: no modal prompt during unattended service startup.
    let success = unsafe {
        CryptProtectData(
            &input,
            ptr::null(),
            ptr::null(),
            ptr::null_mut(),
            ptr::null_mut(),
            0x1,
            &mut output,
        )
    };
    if success == 0 {
        return Err(format!(
            "Windows DPAPI encryption failed: {}",
            std::io::Error::last_os_error()
        ));
    }
    let bytes = unsafe { std::slice::from_raw_parts(output.data, output.len as usize).to_vec() };
    unsafe {
        LocalFree(output.data as *mut c_void);
    }
    Ok(bytes)
}

pub fn decrypt(ciphertext: &[u8]) -> Result<Vec<u8>, String> {
    let input = DataBlob {
        len: ciphertext.len() as u32,
        data: ciphertext.as_ptr() as *mut u8,
    };
    let mut output = DataBlob {
        len: 0,
        data: ptr::null_mut(),
    };
    let success = unsafe {
        CryptUnprotectData(
            &input,
            ptr::null_mut(),
            ptr::null(),
            ptr::null_mut(),
            ptr::null_mut(),
            0x1,
            &mut output,
        )
    };
    if success == 0 {
        return Err("解密失败：Windows 用户身份已改变或密钥文件损坏".into());
    }
    let bytes = unsafe { std::slice::from_raw_parts(output.data, output.len as usize).to_vec() };
    unsafe {
        LocalFree(output.data as *mut c_void);
    }
    Ok(bytes)
}

pub fn read(name: &str) -> Result<Option<String>, String> {
    let file = super::state_dir().join(format!("{name}.dpapi"));
    let ciphertext = match fs::read(file) {
        Ok(b) => b,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(e.to_string()),
    };
    String::from_utf8(decrypt(&ciphertext)?)
        .map(Some)
        .map_err(|e| e.to_string())
}

pub fn store(name: &str, value: &str) -> Result<(), String> {
    fs::create_dir_all(super::state_dir()).map_err(|e| e.to_string())?;
    let file = super::state_dir().join(format!("{name}.dpapi"));
    let tmp = file.with_extension("dpapi.tmp");
    fs::write(&tmp, encrypt(value.as_bytes())?).map_err(|e| e.to_string())?;
    // Windows replace is atomic; ordinary rename fails when destination exists.
    let source: Vec<u16> = tmp.as_os_str().encode_wide().chain(Some(0)).collect();
    let dest: Vec<u16> = file.as_os_str().encode_wide().chain(Some(0)).collect();
    #[link(name = "Kernel32")]
    unsafe extern "system" {
        fn MoveFileExW(from: *const u16, to: *const u16, flags: u32) -> i32;
    }
    let status = unsafe { MoveFileExW(source.as_ptr(), dest.as_ptr(), 0x1 | 0x8) };
    if status == 0 {
        return Err(std::io::Error::last_os_error().to_string());
    }
    Ok(())
}

pub fn random_api_key() -> Result<String, String> {
    let mut bytes = [0_u8; 32];
    let status = unsafe { BCryptGenRandom(ptr::null_mut(), bytes.as_mut_ptr(), 32, 0x2) };
    if status < 0 {
        return Err(format!("Windows cryptographic RNG failed: {status}"));
    }
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut key = String::from("ps-");
    for byte in bytes {
        key.push(HEX[(byte >> 4) as usize] as char);
        key.push(HEX[(byte & 0xf) as usize] as char);
    }
    Ok(key)
}

pub fn ensure_api_key() -> Result<String, String> {
    if let Some(key) = read("api-key")? {
        return Ok(key);
    }
    let key = random_api_key()?;
    store("api-key", &key)?;
    Ok(key)
}
