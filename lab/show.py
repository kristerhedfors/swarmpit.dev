#!/usr/bin/env python3
"""Print a compact transcript for every agent in a run directory: python3 lab/show.py runs/<run_id>"""
import sys
from pathlib import Path

from analysis.transcript import run_transcript


def main():
    for line in run_transcript(Path(sys.argv[1])):
        print(line)


if __name__ == "__main__":
    main()
