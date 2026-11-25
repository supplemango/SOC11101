# https://salsa.debian.org/benchmarksgame-team/benchmarksgame/
#
# contributed by Joerg Baumann
#
# LOCAL MODIFICATION (E1 harness, 2026-10-11): the number of worker processes can be set with the environment variable
# MANDEL_WORKERS (1 = no Pool, a single process; unset or 0 = cpu_count(), as in the original). Nothing else changed.
# Usage: MANDEL_WORKERS=1 python3 mandelbrot_w.py 2000 > /dev/null   (the output is a binary PBM image: never print it)

from contextlib import closing
from itertools import islice
from os import cpu_count, environ
from sys import argv, stdout

def pixels(y, n, abs):
    range7 = bytearray(range(7))
    pixel_bits = bytearray(128 >> pos for pos in range(8))
    c1 = 2. / float(n)
    c0 = -1.5 + 1j * y * c1 - 1j
    x = 0
    while True:
        pixel = 0
        c = x * c1 + c0
        for pixel_bit in pixel_bits:
            z = c
            for _ in range7:
                for _ in range7:
                    z = z * z + c
                if abs(z) >= 2.: break
            else:
                pixel += pixel_bit
            c += c1
        yield pixel
        x += 8

def compute_row(p):
    y, n = p

    result = bytearray(islice(pixels(y, n, abs), (n + 7) // 8))
    result[-1] &= 0xff << (8 - n % 8)
    return y, result

def ordered_rows(rows, n):
    order = [None] * n
    i = 0
    j = n
    while i < len(order):
        if j > 0:
            row = next(rows)
            order[row[0]] = row
            j -= 1

        if order[i]:
            yield order[i]
            order[i] = None
            i += 1

def compute_rows(n, f):
    row_jobs = ((y, n) for y in range(n))

    workers = int(environ.get("MANDEL_WORKERS", "0") or 0) or cpu_count()
    if workers < 2:
        yield from map(f, row_jobs)
    else:
        from multiprocessing import Pool
        with Pool(workers) as pool:
            unordered_rows = pool.imap_unordered(f, row_jobs)
            yield from ordered_rows(unordered_rows, n)

def mandelbrot(n):
    write = stdout.buffer.write

    with closing(compute_rows(n, compute_row)) as rows:
        write("P4\n{0} {0}\n".format(n).encode())
        for row in rows:
            write(row[1])

if __name__ == '__main__':
    mandelbrot(int(argv[1]))
