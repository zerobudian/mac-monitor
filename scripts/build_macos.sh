#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:-1.0.0}"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_DIR/.build-venv/bin/python}"
ARCH="$(uname -m)"
APP_NAME="Mac Monitor"
APP_PATH="$PROJECT_DIR/dist/$APP_NAME.app"
RELEASE_DIR="$PROJECT_DIR/release"
ARCHIVE="$RELEASE_DIR/MacMonitor-$VERSION-macos-$ARCH.zip"
NATIVE_DIR="$PROJECT_DIR/build/native"
NATIVE_BIN="$NATIVE_DIR/mac-monitor-desktop"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This package can only be built on macOS." >&2
  exit 1
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Missing build Python: $PYTHON_BIN" >&2
  echo "Create .build-venv and install PyInstaller first; see README.md." >&2
  exit 1
fi

if ! "$PYTHON_BIN" -m PyInstaller --version >/dev/null 2>&1; then
  echo "PyInstaller is not installed in $PYTHON_BIN." >&2
  exit 1
fi

mkdir -p "$NATIVE_DIR" "$RELEASE_DIR"
rm -rf "$PROJECT_DIR/build/pyinstaller" "$APP_PATH"

/usr/bin/swiftc \
  -O \
  -swift-version 5 \
  -framework Cocoa \
  -framework WebKit \
  "$PROJECT_DIR/desktop.swift" \
  -o "$NATIVE_BIN"

"$PYTHON_BIN" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name "$APP_NAME" \
  --osx-bundle-identifier "io.github.zerobudian.mac-monitor" \
  --add-data "$PROJECT_DIR/web:web" \
  --add-binary "$NATIVE_BIN:." \
  --workpath "$PROJECT_DIR/build/pyinstaller" \
  --specpath "$PROJECT_DIR/build" \
  --distpath "$PROJECT_DIR/dist" \
  "$PROJECT_DIR/server.py"

/usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $VERSION" "$APP_PATH/Contents/Info.plist" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$APP_PATH/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $VERSION" "$APP_PATH/Contents/Info.plist" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$APP_PATH/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$APP_PATH/Contents/Info.plist" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Set :LSUIElement true" "$APP_PATH/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :LSMinimumSystemVersion string 26.0" "$APP_PATH/Contents/Info.plist" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Set :LSMinimumSystemVersion 26.0" "$APP_PATH/Contents/Info.plist"

/usr/bin/codesign --force --deep --sign - "$APP_PATH"
rm -f "$ARCHIVE" "$ARCHIVE.sha256"
/usr/bin/ditto -c -k --sequesterRsrc --keepParent "$APP_PATH" "$ARCHIVE"
/usr/bin/shasum -a 256 "$ARCHIVE" > "$ARCHIVE.sha256"

echo "Built: $ARCHIVE"
echo "SHA-256: $(cut -d ' ' -f 1 "$ARCHIVE.sha256")"
