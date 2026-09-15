#!/bin/sh
set -eu

package_root=/package
output_root=/out

rm -rf "$package_root" "$output_root"
mkdir -p "$package_root/.python_packages/lib/site-packages" "$output_root"

python -m pip install \
    --no-cache-dir \
    --requirement requirements.txt \
    --target "$package_root/.python_packages/lib/site-packages"

cp function_app.py host.json requirements.txt "$package_root/"
for directory in clients core functions services storage web; do
    if [ ! -d "$directory" ]; then
        echo "Required deployment directory is missing: $directory" >&2
        exit 1
    fi
    cp -R "$directory" "$package_root/"
done

PYTHONPATH="$package_root:$package_root/.python_packages/lib/site-packages" \
    python -m compileall -q "$package_root"
PYTHONPATH="$package_root:$package_root/.python_packages/lib/site-packages" \
    python -c 'import function_app; functions = function_app.app.get_functions(); assert functions, "No functions discovered"; print(f"Discovered {len(functions)} functions in deployment package")'

find "$package_root" -type d -name __pycache__ -prune -exec rm -rf '{}' +
find "$package_root" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete

python - "$package_root" "$output_root/az-capacity-function.zip" <<'PY'
from pathlib import Path
import sys
import zipfile

package_root = Path(sys.argv[1])
output_path = Path(sys.argv[2])

with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for path in sorted(package_root.rglob("*")):
        if path.is_file():
            archive.write(path, path.relative_to(package_root).as_posix())
PY

sha256sum "$output_root/az-capacity-function.zip" \
    > "$output_root/az-capacity-function.zip.sha256"