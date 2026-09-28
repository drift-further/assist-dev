"""A small QR encoder for `assist pair`: byte mode, error correction L, versions 1-6.

Up to 134 bytes, which is room for any Assist URL. Written out rather than
pulled in as a dependency; the algorithm follows ISO/IEC 18004 and the
structure of Project Nayuki's reference encoder. `qrencode` on PATH is used in
preference when present (cli/pair.py).
"""

# (total codewords, EC codewords per block, blocks) for level L, versions 1-6.
_VERSIONS = {
    1: (26, 7, 1),
    2: (44, 10, 1),
    3: (70, 15, 1),
    4: (100, 20, 1),
    5: (134, 26, 1),
    6: (172, 18, 2),
}
_FORMAT_BITS_L = 1


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for index, coefficient in enumerate(divisor):
            result[index] ^= _gf_mul(coefficient, factor)
    return result


def _codewords(payload: bytes) -> tuple[int, list[int]]:
    for version, (total, ec_len, blocks) in _VERSIONS.items():
        data_len = total - ec_len * blocks
        if len(payload) + 2 <= data_len:
            break
    else:
        raise ValueError(f"too long for a version 1-6 QR code: {len(payload)} bytes")

    bits = [0, 1, 0, 0]  # byte mode
    bits += [(len(payload) >> i) & 1 for i in reversed(range(8))]
    for byte in payload:
        bits += [(byte >> i) & 1 for i in reversed(range(8))]
    bits += [0] * min(4, data_len * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    data = [int("".join(map(str, bits[i : i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(data) < data_len:
        data.append(pad)
        pad ^= 0xEC ^ 0x11

    block_len = data_len // blocks
    divisor = _rs_divisor(ec_len)
    data_blocks = [data[i * block_len : (i + 1) * block_len] for i in range(blocks)]
    ec_blocks = [_rs_remainder(block, divisor) for block in data_blocks]
    out = [block[i] for i in range(block_len) for block in data_blocks]
    out += [block[i] for i in range(ec_len) for block in ec_blocks]
    return version, out


class _Matrix:
    def __init__(self, version: int):
        self.size = version * 4 + 17
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.function = [[False] * self.size for _ in range(self.size)]
        self._draw_function_patterns(version)

    def set_function(self, x: int, y: int, dark: bool) -> None:
        self.dark[y][x] = dark
        self.function[y][x] = True

    def _draw_function_patterns(self, version: int) -> None:
        size = self.size
        for i in range(size):
            self.set_function(6, i, i % 2 == 0)
            self.set_function(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < size and 0 <= y < size:
                        distance = max(abs(dx), abs(dy))
                        self.set_function(x, y, distance not in (2, 4))
        if version >= 2:
            centre = size - 7
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    self.set_function(centre + dx, centre + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format(0)  # reserve the format areas; real bits drawn later

    def draw_format(self, mask: int) -> None:
        size = self.size
        data = _FORMAT_BITS_L << 3 | mask
        remainder = data
        for _ in range(10):
            remainder = (remainder << 1) ^ ((remainder >> 9) * 0x537)
        bits = (data << 10 | remainder) ^ 0x5412

        def bit(i):
            return (bits >> i) & 1 != 0

        for i in range(6):
            self.set_function(8, i, bit(i))
        self.set_function(8, 7, bit(6))
        self.set_function(8, 8, bit(7))
        self.set_function(7, 8, bit(8))
        for i in range(9, 15):
            self.set_function(14 - i, 8, bit(i))
        for i in range(8):
            self.set_function(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set_function(8, size - 15 + i, bit(i))
        self.set_function(8, size - 8, True)

    def place(self, codewords: list[int]) -> None:
        size = self.size
        total_bits = len(codewords) * 8
        index = 0
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vertical in range(size):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = size - 1 - vertical if upward else vertical
                    if not self.function[y][x] and index < total_bits:
                        byte = codewords[index >> 3]
                        self.dark[y][x] = (byte >> (7 - (index & 7))) & 1 != 0
                        index += 1
            right -= 2

    def apply_mask(self, mask: int) -> None:
        for y in range(self.size):
            for x in range(self.size):
                if not self.function[y][x] and _MASKS[mask](x, y):
                    self.dark[y][x] = not self.dark[y][x]


_MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


def _penalty(grid: list[list[bool]]) -> int:
    size = len(grid)
    score = 0
    lines = [row for row in grid] + [[grid[y][x] for y in range(size)] for x in range(size)]
    finder_like = ("10111010000", "00001011101")
    for line in lines:
        run = 1
        for i in range(1, size + 1):
            if i < size and line[i] == line[i - 1]:
                run += 1
                continue
            if run >= 5:
                score += run - 2
            run = 1
        text = "".join("1" if cell else "0" for cell in line)
        score += 40 * sum(text.count(pattern) for pattern in finder_like)
    for y in range(size - 1):
        for x in range(size - 1):
            if grid[y][x] == grid[y][x + 1] == grid[y + 1][x] == grid[y + 1][x + 1]:
                score += 3
    dark = sum(sum(row) for row in grid)
    score += 10 * (abs(dark * 20 - size * size * 10) // (size * size))
    return score


def encode(text: str) -> list[list[bool]]:
    """Return the module grid (True = dark) for `text`, quiet zone excluded."""
    version, codewords = _codewords(text.encode("utf-8"))
    best = None
    for mask in range(8):
        matrix = _Matrix(version)
        matrix.place(codewords)
        matrix.apply_mask(mask)
        matrix.draw_format(mask)
        score = _penalty(matrix.dark)
        if best is None or score < best[0]:
            best = (score, matrix.dark)
    return best[1]


def render(grid: list[list[bool]], quiet: int = 2) -> str:
    """Half-block text, two module rows per line, LIGHT modules drawn as ink.

    Drawn light-on-dark with explicit colours so it scans the same whatever the
    terminal theme: foreground white for light modules, background black for
    dark ones.
    """
    size = len(grid) + 2 * quiet

    def light(x, y):
        x -= quiet
        y -= quiet
        inside = 0 <= x < len(grid) and 0 <= y < len(grid)
        return not (inside and grid[y][x])

    rows = []
    for y in range(0, size, 2):
        chars = []
        for x in range(size):
            top = light(x, y)
            bottom = light(x, y + 1) if y + 1 < size else False
            chars.append({(True, True): "█", (True, False): "▀", (False, True): "▄"}.get(
                (top, bottom), " "
            ))
        rows.append("\033[97;40m" + "".join(chars) + "\033[0m")
    return "\n".join(rows)
