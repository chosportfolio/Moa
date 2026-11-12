"""Build the local public-place index from a previously downloaded OSM file."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from poi_index import build_index, DEFAULT_DB_PATH, DEFAULT_SOURCE_URL


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path, help='Existing .osm.pbf or .osm file')
    parser.add_argument('--output', type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument('--source-url', default=DEFAULT_SOURCE_URL)
    parser.add_argument('--coverage', default='대한민국 공개 지도 자료')
    options = parser.parse_args()
    source, output = options.input.resolve(), options.output.resolve()
    # This is a standalone builder process. Make the source filename relative
    # for libosmium on Windows, where the project's parent path is Korean.
    os.chdir(source.parent)
    metadata = build_index(source.name, output, source_url=options.source_url,
                           coverage=options.coverage)
    print(json.dumps({'output':str(output), **metadata}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
