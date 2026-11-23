#!/usr/bin/env bash
set -euo pipefail

# Build the Quartz web bundle, sync it into Capacitor, and compile the debug APK.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEBAPP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$WEBAPP_DIR/.." && pwd)"

# Resolve JDK 21 (Gradle 8.14 cannot configure build scripts on JDK 25+)
if [[ -z "${JAVA_HOME:-}" ]] || [[ ! -x "${JAVA_HOME}/bin/javac" ]]; then
  for candidate in "$REPO_ROOT/.tooling/jdk21" /tmp/jdk21 /usr/lib/jvm/java-21-openjdk-amd64 /usr/lib/jvm/temurin-21-jdk-amd64; do
    if [[ -x "$candidate/bin/javac" ]]; then
      export JAVA_HOME="$candidate"
      break
    fi
  done
fi

if [[ -z "${JAVA_HOME:-}" ]] || [[ ! -x "${JAVA_HOME}/bin/javac" ]]; then
  echo "Downloading Temurin JDK 21 into .tooling/jdk21..."
  mkdir -p "$REPO_ROOT/.tooling/jdk21"
  JDK_URL=$(curl -sL 'https://api.adoptium.net/v3/assets/latest/21/hotspot?architecture=x64&image_type=jdk&os=linux&vendor=eclipse' | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['binary']['package']['link'])")
  curl -sL "$JDK_URL" -o /tmp/jdk21.tar.gz
  tar xzf /tmp/jdk21.tar.gz -C "$REPO_ROOT/.tooling/jdk21" --strip-components=1
  rm -f /tmp/jdk21.tar.gz
  export JAVA_HOME="$REPO_ROOT/.tooling/jdk21"
fi

# Resolve Android SDK
if [[ -z "${ANDROID_HOME:-}" ]] || [[ ! -d "${ANDROID_HOME}/platforms" ]]; then
  for candidate in "$REPO_ROOT/.tooling/android-sdk" "/tmp/android-sdk" "$HOME/Android/Sdk"; do
    if [[ -d "$candidate/platforms" ]]; then
      export ANDROID_HOME="$candidate"
      break
    fi
  done
fi

if [[ -z "${ANDROID_HOME:-}" ]] || [[ ! -d "${ANDROID_HOME}/platforms" ]]; then
  echo "Error: Android SDK not found. Set ANDROID_HOME or install into .tooling/android-sdk." >&2
  exit 1
fi

export ANDROID_USER_HOME="${ANDROID_USER_HOME:-$REPO_ROOT/.tooling/android-user}"
export GRADLE_USER_HOME="${GRADLE_USER_HOME:-/tmp/gradle-home}"

cd "$WEBAPP_DIR"
npm run build
npx cap sync android

cd "$WEBAPP_DIR/android"
./gradlew assembleDebug --no-daemon

APK_PATH="$WEBAPP_DIR/android/app/build/outputs/apk/debug/app-debug.apk"
echo "APK ready: $APK_PATH ($(du -h "$APK_PATH" | cut -f1))"
