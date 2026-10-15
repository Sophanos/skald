"""QR codes for install links, standard library only: byte mode, versions 1-10, levels L and M."""

# Per version: (EC codewords per block, [(blocks, data codewords per block), ...]).
BLOCKS = {
    'L': [(7, [(1, 19)]), (10, [(1, 34)]), (15, [(1, 55)]), (20, [(1, 80)]), (26, [(1, 108)]),
          (18, [(2, 68)]), (20, [(2, 78)]), (24, [(2, 97)]), (30, [(2, 116)]), (18, [(2, 68), (2, 69)])],
    'M': [(10, [(1, 16)]), (16, [(1, 28)]), (26, [(1, 44)]), (18, [(2, 32)]), (24, [(2, 43)]),
          (16, [(4, 27)]), (18, [(4, 31)]), (22, [(2, 38), (2, 39)]), (22, [(3, 36), (2, 37)]), (26, [(4, 43), (1, 44)])],
}
FORMAT_BITS = {'L': 1, 'M': 0}
ALIGN = [[], [6, 18], [6, 22], [6, 26], [6, 30], [6, 34], [6, 22, 38], [6, 24, 42], [6, 26, 46], [6, 28, 50]]
MASKS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: r * c % 2 + r * c % 3 == 0,
    lambda r, c: (r * c % 2 + r * c % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + r * c % 3) % 2 == 0,
]

EXP, LOG = [0] * 512, [0] * 256
_x = 1
for _i in range(255):
    EXP[_i], LOG[_x] = _x, _i
    _x = _x << 1 ^ (0x11D if _x & 0x80 else 0)
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]


def _ec(data, n):
    """Reed-Solomon remainder of data over the degree-n generator."""
    gen = [1]
    for i in range(n):
        gen = [a ^ (EXP[LOG[b] + i] if b else 0) for a, b in zip(gen + [0], [0] + gen)]
    rem = list(data) + [0] * n
    for i in range(len(data)):
        if rem[i]:
            factor = LOG[rem[i]]
            for j, g in enumerate(gen):
                if g:
                    rem[i + j] ^= EXP[factor + LOG[g]]
    return rem[len(data):]


def _codewords(data, version, level):
    ec, groups = BLOCKS[level][version - 1]
    capacity = sum(n * size for n, size in groups)
    bits = [0, 1, 0, 0] + [len(data) >> i & 1 for i in range(15 if version > 9 else 7, -1, -1)]
    for byte in data:
        bits += [byte >> i & 1 for i in range(7, -1, -1)]
    bits += [0] * min(4, capacity * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    words = [int(''.join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    words += [0xEC, 0x11] * ((capacity - len(words)) // 2) + [0xEC] * ((capacity - len(words)) % 2)
    blocks, start = [], 0
    for n, size in groups:
        for _ in range(n):
            blocks.append(words[start:start + size])
            start += size
    eccs = [_ec(block, ec) for block in blocks]
    out = [b[i] for i in range(max(map(len, blocks))) for b in blocks if i < len(b)]
    return out + [e[i] for i in range(ec) for e in eccs]


def _penalty(m):
    size, score = len(m), 0
    lines = m + [list(col) for col in zip(*m)]
    for line in lines:
        run = 1
        for i in range(1, size + 1):
            if i < size and line[i] == line[i - 1]:
                run += 1
            else:
                score += run - 2 if run >= 5 else 0
                run = 1
        text = ''.join('1' if v else '0' for v in line)
        score += 40 * sum(text.startswith(p, i) for p in ('10111010000', '00001011101') for i in range(size - 10))
    score += 3 * sum(m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]
                     for r in range(size - 1) for c in range(size - 1))
    dark = sum(map(sum, m))
    return score + 10 * (abs(dark * 20 - size * size * 10) // (size * size))


def matrix(text, level='M', mask=None):
    """Return the QR code for text as rows of booleans (True is dark), without a quiet zone."""
    data = text.encode('utf-8')
    version = next((v for v in range(1, 11) if 4 + (16 if v > 9 else 8) + 8 * len(data)
                    <= 8 * sum(n * size for n, size in BLOCKS[level][v - 1][1])), None)
    if version is None:
        raise ValueError('text too long for a QR code')
    size = 17 + 4 * version
    m = [[False] * size for _ in range(size)]
    fixed = [[False] * size for _ in range(size)]

    def put(x, y, dark):
        m[y][x], fixed[y][x] = dark, True

    for i in range(size):
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                if 0 <= cx + dx < size and 0 <= cy + dy < size:
                    put(cx + dx, cy + dy, max(abs(dx), abs(dy)) not in (2, 4))
    spots = ALIGN[version - 1]
    for i, ay in enumerate(spots):
        for j, ax in enumerate(spots):
            if (i, j) not in ((0, 0), (0, len(spots) - 1), (len(spots) - 1, 0)):
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        put(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)

    def format_bits(mask):
        value = FORMAT_BITS[level] << 3 | mask
        rem = value
        for _ in range(10):
            rem = rem << 1 ^ (rem >> 9) * 0x537
        bits = (value << 10 | rem) ^ 0x5412
        bit = [bits >> i & 1 == 1 for i in range(15)]
        for i in range(6):
            put(8, i, bit[i])
        put(8, 7, bit[6])
        put(8, 8, bit[7])
        put(7, 8, bit[8])
        for i in range(9, 15):
            put(14 - i, 8, bit[i])
        for i in range(8):
            put(size - 1 - i, 8, bit[i])
        for i in range(8, 15):
            put(8, size - 15 + i, bit[i])
        put(8, size - 8, True)

    format_bits(0)
    if version >= 7:
        rem = version
        for _ in range(12):
            rem = rem << 1 ^ (rem >> 11) * 0x1F25
        bits = version << 12 | rem
        for i in range(18):
            a, b = size - 11 + i % 3, i // 3
            put(a, b, bits >> i & 1 == 1)
            put(b, a, bits >> i & 1 == 1)
    words = _codewords(data, version, level)
    i, right = 0, size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for x in (right, right - 1):
                y = size - 1 - vert if (right + 1) & 2 == 0 else vert
                if not fixed[y][x] and i < len(words) * 8:
                    m[y][x] = words[i >> 3] >> (7 - (i & 7)) & 1 == 1
                    i += 1
        right -= 2

    def masked(mask):
        rule = MASKS[mask]
        out = [[v ^ (not fixed[r][c] and rule(r, c)) for c, v in enumerate(row)] for r, row in enumerate(m)]
        saved = [row[:] for row in m]
        m[:] = out
        format_bits(mask)
        out = [row[:] for row in m]
        m[:] = saved
        return out

    if mask is not None:
        return masked(mask)
    return min((masked(k) for k in range(8)), key=_penalty)


def terminal(text, level='M'):
    """Render text as a QR code for a terminal: two rows per line, black on white, 4-module quiet zone."""
    m = matrix(text, level)
    size = len(m) + 8
    dark = lambda r, c: 4 <= r < size - 4 and 4 <= c < size - 4 and m[r - 4][c - 4]
    chars = {(False, False): ' ', (True, False): '▀', (False, True): '▄', (True, True): '█'}
    return '\n'.join('\x1b[30;107m' + ''.join(chars[dark(r, c), dark(r + 1, c)] for c in range(size)) + '\x1b[0m'
                     for r in range(0, size, 2))
