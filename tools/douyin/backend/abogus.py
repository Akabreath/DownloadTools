#!/usr/bin/env python3
"""
抖音 web 接口 a_bogus 签名参数生成 (纯 Python, 无第三方依赖)

算法来源: 公开的抖音 web 端签名逆向实现 (a_bogus), 本文件为 Python 移植:
  - 参考实现: https://github.com/amxsa/douyin_abogus_python (abogus.py)
  - 同一算法亦见 f2 / Douyin_TikTok_Download_API 等开源项目
原实现依赖 gmssl 计算 SM3, 这里内置纯 Python SM3 (GB/T 32905-2016),
避免给 DownloadTools 增加第三方依赖 (venv 无 pip, 见项目约定)。

用法:
    from abogus import ABogus
    params = {"aweme_id": "7...", "device_platform": "webapp", ...}   # 有序 dict, 顺序即发送顺序
    a_bogus = ABogus(UA).generate_a_bogus(params)
    # 注意: 必须用同一个 urlencode(params) 拼接真实请求 query, a_bogus 追加在末尾
"""
from random import choice, randint, random
from time import time
from urllib.parse import urlencode

# ---------------- 纯 Python SM3 ----------------
_IV = [0x7380166F, 0x4914B2B9, 0x172442D7, 0xDA8A0600,
       0xA96F30BC, 0x163138AA, 0xE38DEE4D, 0xB0FB0E4E]


def _rotl(x: int, n: int) -> int:
    n %= 32
    return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF


def _p0(x: int) -> int:
    return x ^ _rotl(x, 9) ^ _rotl(x, 17)


def _p1(x: int) -> int:
    return x ^ _rotl(x, 15) ^ _rotl(x, 23)


def _cf(v: list, block: bytes) -> list:
    w = [int.from_bytes(block[i * 4:i * 4 + 4], "big") for i in range(16)]
    for j in range(16, 68):
        w.append(_p1(w[j - 16] ^ w[j - 9] ^ _rotl(w[j - 3], 15))
                 ^ _rotl(w[j - 13], 7) ^ w[j - 6])
    w1 = [w[j] ^ w[j + 4] for j in range(64)]
    a, b, c, d, e, f, g, h = v
    for j in range(64):
        t = 0x79CC4519 if j < 16 else 0x7A879D8A
        ss1 = _rotl((_rotl(a, 12) + e + _rotl(t, j)) & 0xFFFFFFFF, 7)
        ss2 = ss1 ^ _rotl(a, 12)
        if j < 16:
            ff = a ^ b ^ c
            gg = e ^ f ^ g
        else:
            ff = (a & b) | (a & c) | (b & c)
            gg = (e & f) | (~e & g & 0xFFFFFFFF)
        tt1 = (ff + d + ss2 + w1[j]) & 0xFFFFFFFF
        tt2 = (gg + h + ss1 + w[j]) & 0xFFFFFFFF
        d, c, b, a = c, _rotl(b, 9), a, tt1
        h, g, f, e = g, _rotl(f, 19), e, _p0(tt2)
    return [v[i] ^ x for i, x in enumerate((a, b, c, d, e, f, g, h))]


def sm3_hash(data: bytes) -> bytes:
    """SM3 摘要, 返回 32 字节"""
    v = list(_IV)
    bit_len = len(data) * 8
    data = data + b"\x80"
    while len(data) % 64 != 56:
        data += b"\x00"
    data += bit_len.to_bytes(8, "big")
    for i in range(0, len(data), 64):
        v = _cf(v, data[i:i + 64])
    return b"".join(x.to_bytes(4, "big") for x in v)


# ---------------- a_bogus ----------------
class ABogus:
    __end_string = "cus"
    __str = {
        "s0": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=",
        "s1": "Dkdpgh4ZKsQB80/Mfvw36XI1R25+WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe=",
        "s2": "Dkdpgh4ZKsQB80/Mfvw36XI1R25-WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe=",
        "s3": "ckdp1h4ZKsUB80/Mfvw36XIgR25+WQAlEi7NLboqYTOPuzmFjJnryx9HVGDaStCe",
        "s4": "Dkdpgh2ZmsQB80/MfvV36XI1R45-WUAlEixNLwoqYTOPuzKFjJnry79HbGcaStCe",
    }

    def __init__(self, user_agent: str = "", platform: str = "Win32"):
        self.user_agent = user_agent
        self.ua_code = self.generate_ua_code(user_agent)
        self.browser = self.generate_browser_info(platform)
        self.browser_len = len(self.browser)
        self.browser_code = self.char_code_at(self.browser)

    # -------- 基础工具 --------
    def generate_ua_code(self, user_agent: str) -> list:
        numbers = [0.00390625, 1, 14]
        key_string = "".join(chr(int(num)) for num in numbers)
        return self.sm3_to_array(
            self.generate_result(self.rc4_encrypt(user_agent, key_string), "s3"))

    def random_list(self, b=170, c=85, d=0, e=0, f=0, g=0) -> list:
        r = random() * 10000
        v = [r, int(r) & 255, int(r) >> 8]
        v.append(v[1] & b | d)
        v.append(v[1] & c | e)
        v.append(v[2] & b | f)
        v.append(v[2] & c | g)
        return v[-4:]

    def list_1(self, a=170, b=85, c=45) -> list:
        return self.random_list(a, b, 1, 2, 5, c & a)

    def list_2(self, a=170, b=85) -> list:
        return self.random_list(a, b, 1, 0, 0, 0)

    def list_3(self, a=170, b=85) -> list:
        return self.random_list(a, b, 1, 0, 5, 0)

    def from_char_code(self, *args) -> str:
        return "".join(chr(code) for code in args)

    def generate_string_1(self) -> str:
        return (self.from_char_code(*self.list_1())
                + self.from_char_code(*self.list_2())
                + self.from_char_code(*self.list_3()))

    def generate_string_2(self, url_params: str, method="GET") -> str:
        a = self.generate_string_2_list(url_params, method)
        e = self.end_check_num(a)
        a.extend(self.browser_code)
        a.append(e)
        return self.rc4_encrypt(self.from_char_code(*a), "y")

    def generate_string_2_list(self, url_params: str, method="GET") -> list:
        start_time = int(time() * 1000)
        end_time = start_time + randint(4, 8)
        params_array = self.generate_params_code(url_params)
        method_array = self.generate_method_code(method)
        return self.list_4(
            (end_time >> 24) & 255, params_array[21], self.ua_code[23],
            (end_time >> 16) & 255, params_array[22], self.ua_code[24],
            (end_time >> 8) & 255, (end_time >> 0) & 255,
            (start_time >> 24) & 255, (start_time >> 16) & 255,
            (start_time >> 8) & 255, (start_time >> 0) & 255,
            method_array[21], method_array[22],
            int(end_time / 256 / 256 / 256 / 256) >> 0,
            int(start_time / 256 / 256 / 256 / 256) >> 0,
            self.browser_len,
        )

    def list_4(self, a, b, c, d, e, f, g, h, i, j, k, m, n, o, p, q, r) -> list:
        return [
            44, a, 0, 0, 0, 0, 24, b, n, 0, c, d, 0, 0, 0, 1, 0, 239, e, o, f, g,
            0, 0, 0, 0, h, 0, 0, 14, i, j, 0, k, m, 3, p, 1, q, 1, r, 0, 0, 0]

    def end_check_num(self, a: list) -> int:
        r = 0
        for i in a:
            r ^= i
        return r

    def char_code_at(self, s: str) -> list:
        return [ord(char) for char in s]

    def generate_result(self, s: str, e="s4") -> str:
        r = []
        for i in range(0, len(s), 3):
            if i + 2 < len(s):
                n = (ord(s[i]) << 16) | (ord(s[i + 1]) << 8) | ord(s[i + 2])
            elif i + 1 < len(s):
                n = (ord(s[i]) << 16) | (ord(s[i + 1]) << 8)
            else:
                n = ord(s[i]) << 16
            for j, k in zip(range(18, -1, -6), (0xFC0000, 0x03F000, 0x0FC0, 0x3F)):
                if j == 6 and i + 1 >= len(s):
                    break
                if j == 0 and i + 2 >= len(s):
                    break
                r.append(self.__str[e][(n & k) >> j])
        r.append("=" * ((4 - len(r) % 4) % 4))
        return "".join(r)

    def generate_method_code(self, method: str = "GET") -> list:
        return self.sm3_to_array(self.sm3_to_array(method + self.__end_string))

    def generate_params_code(self, params: str) -> list:
        return self.sm3_to_array(self.sm3_to_array(params + self.__end_string))

    def sm3_to_array(self, data) -> list:
        b = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        h = sm3_hash(b).hex()
        return [int(h[i:i + 2], 16) for i in range(0, len(h), 2)]

    def generate_browser_info(self, platform: str = "Win32") -> str:
        inner_width = randint(1280, 1920)
        inner_height = randint(720, 1080)
        outer_width = randint(inner_width, 1920)
        outer_height = randint(inner_height, 1080)
        screen_x = 0
        screen_y = choice((0, 30))
        value_list = [inner_width, inner_height, outer_width, outer_height,
                      screen_x, screen_y, 0, 0, outer_width, outer_height,
                      outer_width, outer_height, inner_width, inner_height,
                      24, 24, platform]
        return "|".join(str(i) for i in value_list)

    def rc4_encrypt(self, plaintext: str, key: str) -> str:
        s = list(range(256))
        j = 0
        for i in range(256):
            j = (j + s[i] + ord(key[i % len(key)])) % 256
            s[i], s[j] = s[j], s[i]
        i = 0
        j = 0
        cipher = []
        for k in range(len(plaintext)):
            i = (i + 1) % 256
            j = (j + s[i]) % 256
            s[i], s[j] = s[j], s[i]
            t = (s[i] + s[j]) % 256
            cipher.append(chr(s[t] ^ ord(plaintext[k])))
        return "".join(cipher)

    # -------- 对外入口 --------
    def generate_a_bogus(self, url_params) -> str:
        """url_params: dict (有序, 顺序即请求 query 顺序) 或 str"""
        if not isinstance(url_params, str):
            url_params = urlencode(url_params)
        string_1 = self.generate_string_1()
        string_2 = self.generate_string_2(url_params)
        return self.generate_result(string_1 + string_2, "s4")
