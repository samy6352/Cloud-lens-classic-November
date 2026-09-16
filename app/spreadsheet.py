from __future__ import annotations

import csv


def safe_cell(value):
    if isinstance(value, str) and value.lstrip(" \t\r\n\ufeff").startswith(("=", "+", "-", "@")):
        return "'" + value
    if isinstance(value, str) and value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


class SafeCsvWriter:
    def __init__(self, output, **kwargs):
        self.writer = csv.writer(output, **kwargs)

    def writerow(self, row):
        return self.writer.writerow([safe_cell(value) for value in row])

    def writerows(self, rows):
        for row in rows:
            self.writerow(row)
