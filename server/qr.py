"""QR codes for the Listen page posters, made on the C3 Life server with no internet and nothing to install.

Byte mode, error correction level M (about 15% of the code can be dirty or torn and it still reads), versions 1 to 10,
which is up to 213 letters: plenty for http://192.168.10.10/listen/kids. svg() gives a picture to print.
"""

# Error correction level M: (data codewords per block, number of blocks), ...; EC codewords per block
BLOCKS_M = {1: ([(16, 1)], 10), 2: ([(28, 1)], 16), 3: ([(44, 1)], 26), 4: ([(32, 2)], 18), 5: ([(43, 2)], 24),
            6: ([(27, 4)], 16), 7: ([(31, 4)], 18), 8: ([(38, 2), (39, 2)], 22), 9: ([(36, 3), (37, 2)], 22),
            10: ([(43, 4), (44, 1)], 26)}
ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
         9: [6, 26, 46], 10: [6, 28, 50]}

EXP, LOG = [0] * 512, [0] * 256
_x = 1
for _i in range(255):
    EXP[_i], LOG[_x] = _x, _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]


def _mul(a, b):
    return 0 if a == 0 or b == 0 else EXP[LOG[a] + LOG[b]]


def _ec(data, n):
    """Reed-Solomon: the n error correction codewords for these data codewords."""
    gen = [1]
    for i in range(n):  # (x - a^0)(x - a^1)...(x - a^(n-1))
        gen = [a ^ _mul(b, EXP[i]) for a, b in zip(gen + [0], [0] + gen)]
    rem = list(data) + [0] * n
    for i in range(len(data)):
        c = rem[i]
        if c:
            for j in range(1, n + 1):
                rem[i + j] ^= _mul(gen[j], c)
    return rem[len(data):]


def _bch(value, poly, bits):
    v = value << bits
    top = poly.bit_length() - 1
    while v.bit_length() - 1 >= top:
        v ^= poly << (v.bit_length() - 1 - top)
    return (value << bits) | v


MASKS = [lambda r, c: (r + c) % 2 == 0, lambda r, c: r % 2 == 0, lambda r, c: c % 3 == 0, lambda r, c: (r + c) % 3 == 0,
         lambda r, c: (r // 2 + c // 3) % 2 == 0, lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
         lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0, lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0]


def _codewords(data, version):
    groups, ecn = BLOCKS_M[version]
    total = sum(d * n for d, n in groups)
    bits = [0, 1, 0, 0]  # byte mode
    count_bits = 8 if version < 10 else 16
    bits += [(len(data) >> i) & 1 for i in range(count_bits - 1, -1, -1)]
    for b in data:
        bits += [(b >> i) & 1 for i in range(7, -1, -1)]
    bits += [0] * min(4, total * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    used = len(words)
    while len(words) < total:  # fill with 236, 17, 236, 17...
        words.append((0xEC, 0x11)[(len(words) - used) % 2])
    blocks, k = [], 0
    for d, n in groups:
        for _ in range(n):
            blocks.append(words[k:k + d])
            k += d
    ecs = [_ec(b, ecn) for b in blocks]
    out = []
    for i in range(max(len(b) for b in blocks)):
        out += [b[i] for b in blocks if i < len(b)]
    for i in range(ecn):
        out += [e[i] for e in ecs]
    return out


def _penalty(m):
    n, p = len(m), 0
    for rows in (m, [list(c) for c in zip(*m)]):  # runs of five or more the same, across and down
        for row in rows:
            run, last = 0, None
            for v in row:
                if v == last:
                    run += 1
                else:
                    if run >= 5:
                        p += run - 2
                    run, last = 1, v
            if run >= 5:
                p += run - 2
    for r in range(n - 1):  # 2x2 blocks the same
        for c in range(n - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                p += 3
    pat = [1, 0, 1, 1, 1, 0, 1]  # finder-like patterns with four light modules either side
    for rows in (m, [list(c) for c in zip(*m)]):
        for row in rows:
            for c in range(n - 6):
                if row[c:c + 7] == pat:
                    before, after = row[max(0, c - 4):c], row[c + 7:c + 11]
                    if (len(before) == 4 and not any(before)) or (len(after) == 4 and not any(after)):
                        p += 40
    dark = sum(map(sum, m))
    p += abs(dark * 100 // (n * n) - 50) // 5 * 10
    return p


def matrix(text):
    """The QR code for `text` as rows of 1 (dark) and 0 (light), without the quiet zone."""
    data = text.encode("utf-8")
    version = next((v for v in range(1, 11)
                    if len(data) * 8 + 4 + (8 if v < 10 else 16) <= sum(d * n for d, n in BLOCKS_M[v][0]) * 8), None)
    if not version:
        raise ValueError("Too long for a QR code here (213 letters at most).")
    n = 17 + 4 * version
    m = [[0] * n for _ in range(n)]
    fixed = [[False] * n for _ in range(n)]

    def put(r, c, v):
        m[r][c], fixed[r][c] = v, True

    for fr, fc in ((0, 0), (0, n - 7), (n - 7, 0)):  # the three corner squares, with their light edges
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = fr + r, fc + c
                if 0 <= rr < n and 0 <= cc < n:
                    on = 0 <= r <= 6 and 0 <= c <= 6 and (r in (0, 6) or c in (0, 6) or (2 <= r <= 4 and 2 <= c <= 4))
                    put(rr, cc, 1 if on else 0)
    for i in range(8, n - 8):  # timing lines
        put(6, i, 1 - i % 2)
        put(i, 6, 1 - i % 2)
    pos = ALIGN[version]
    for r in pos:
        for c in pos:
            if (r, c) in ((pos[0], pos[0]), (pos[0], pos[-1]), (pos[-1], pos[0])):  # where the corner squares are
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    put(r + dr, c + dc, 1 if max(abs(dr), abs(dc)) != 1 else 0)
    put(n - 8, 8, 1)  # the dark module
    for i in range(9):  # room for the format information
        for r, c in ((8, i), (i, 8)):
            if not fixed[r][c]:
                fixed[r][c] = True
    for i in range(8):
        fixed[8][n - 1 - i] = True
        fixed[n - 1 - i][8] = True
    if version >= 7:
        vi = _bch(version, 0x1F25, 12)
        for i in range(18):
            b = (vi >> i) & 1
            put(n - 11 + i % 3, i // 3, b)
            put(i // 3, n - 11 + i % 3, b)

    bits = []
    for w in _codewords(data, version):
        bits += [(w >> i) & 1 for i in range(7, -1, -1)]
    k, up = 0, True
    c = n - 1
    while c > 0:
        if c == 6:
            c -= 1
        for i in range(n):
            r = n - 1 - i if up else i
            for cc in (c, c - 1):
                if not fixed[r][cc]:
                    m[r][cc] = bits[k] if k < len(bits) else 0
                    k += 1
        up = not up
        c -= 2

    best = None
    for mask in range(8):
        t = [row[:] for row in m]
        for r in range(n):
            for cc in range(n):
                if not fixed[r][cc] and MASKS[mask](r, cc):
                    t[r][cc] ^= 1
        f = _bch((0b00 << 3) | mask, 0x537, 10) ^ 0x5412  # level M is 00
        fb = [(f >> i) & 1 for i in range(15)]
        for i in range(15):  # around the top left corner, then split between the other two
            if i < 6:
                t[i][8] = fb[i]
            elif i < 8:
                t[i + 1][8] = fb[i]
            else:
                t[8][14 - i if i > 8 else 7] = fb[i]
            if i < 8:
                t[8][n - 1 - i] = fb[i]
            else:
                t[n - 15 + i][8] = fb[i]
        t[n - 8][8] = 1
        score = _penalty(t)
        if best is None or score < best[0]:
            best = (score, t)
    return best[1]


def svg(text, size_mm=100):
    """A printable picture of the QR code, with the light border (quiet zone) readers need."""
    m = matrix(text)
    n, q = len(m), 4
    path = "".join("M%d %dh1v1h-1z" % (c + q, r + q) for r in range(n) for c in range(n) if m[r][c])
    w = n + 2 * q
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%dmm" height="%dmm" shape-rendering="crispEdges">'
            '<rect width="100%%" height="100%%" fill="#fff"/><path d="%s" fill="#000"/></svg>' % (w, w, size_mm, size_mm, path))
