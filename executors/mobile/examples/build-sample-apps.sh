#!/bin/bash
# Builds one Flutter app and one React Native app for the venom mobile tests (#2400, #2398).
# Installs only what is missing (Flutter SDK, CocoaPods), then builds from the frameworks' own project templates.
# Output apps (iOS simulator, arm64) and zips are listed at the end. Log: ~/venom-mobile/hybrid/setup.log
# Run on the Mac:  nohup bash ~/venom-mobile/build-sample-apps.sh > ~/venom-mobile/setup.out 2>&1 &
#
# First run of Flutter: do it once by hand in a Terminal on the Mac itself (run `flutter --version`).
# macOS Gatekeeper can stall the Dart binary when it is started over SSH, with no prompt visible to you.
# See executors/mobile/README.md, "Set up the Mac".
export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8   # CocoaPods fails without a UTF-8 locale
set -u
export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH
W="$HOME/venom-mobile/hybrid"
mkdir -p "$W"
cd "$W" || exit 1
exec > >(tee -a "$W/setup.log") 2>&1
step() { echo; echo "=== $(date +%T) $*"; }

step "tools"
command -v flutter >/dev/null || brew install --cask flutter
command -v pod >/dev/null || brew install cocoapods
flutter --version
pod --version

step "flutter app (template counter app)"
[ -d venom_flutter ] || flutter create --org dev.venomtest --project-name venom_flutter venom_flutter
(
  cd venom_flutter || exit 1
  flutter precache --ios
  # Flutter builds simulator apps in debug mode only (release is not supported for simulators).
  flutter build ios --simulator --debug
  # Android release, arm64 only (the emulator on this Mac is arm64): best effort, needs the Android SDK.
  export ANDROID_HOME="${ANDROID_HOME:-/opt/homebrew/share/android-commandlinetools}"
  flutter config --android-sdk "$ANDROID_HOME" >/dev/null 2>&1
  yes | flutter doctor --android-licenses >/dev/null 2>&1
  flutter build apk --release --target-platform android-arm64 || echo "ANDROID FLUTTER BUILD SKIPPED (see log)"
) || echo "FLUTTER BUILD FAILED (see log)"

step "react native app (init template)"
[ -d VenomRN ] || npx --yes @react-native-community/cli@latest init VenomRN --skip-git-init
(
  cd VenomRN/ios || exit 1
  pod install
  cd ..
  xcodebuild -workspace ios/VenomRN.xcworkspace -scheme VenomRN -configuration Release \
    -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' \
    -derivedDataPath ios/build ARCHS=arm64 ONLY_ACTIVE_ARCH=NO build
  # Android release, arm64 only: best effort.
  export ANDROID_HOME="${ANDROID_HOME:-/opt/homebrew/share/android-commandlinetools}"
  (cd android && ./gradlew assembleRelease -PreactNativeArchitectures=arm64-v8a) || echo "ANDROID RN BUILD SKIPPED (see log)"
) || echo "REACT NATIVE BUILD FAILED (see log)"

step "collect"
mkdir -p "$HOME/venom-mobile/apps"
FL="venom_flutter/build/ios/iphonesimulator/Runner.app"
RN="VenomRN/ios/build/Build/Products/Release-iphonesimulator/VenomRN.app"
[ -d "$FL" ] && ditto -c -k --sequesterRsrc --keepParent "$FL" "$HOME/venom-mobile/apps/FlutterDemo.app.zip"
[ -d "$RN" ] && ditto -c -k --sequesterRsrc --keepParent "$RN" "$HOME/venom-mobile/apps/ReactNativeDemo.app.zip"
cp venom_flutter/build/app/outputs/flutter-apk/app-release.apk "$HOME/venom-mobile/apps/FlutterDemo-arm64.apk" 2>/dev/null
cp VenomRN/android/app/build/outputs/apk/release/app-release.apk "$HOME/venom-mobile/apps/ReactNativeDemo-arm64.apk" 2>/dev/null
echo "--- apps"; ls -la "$HOME/venom-mobile/apps"
for z in "$HOME/venom-mobile/apps"/*Demo*.zip; do [ -f "$z" ] && echo "$z"; done
for a in "$FL" "$RN"; do [ -d "$a" ] && { x=$(/usr/libexec/PlistBuddy -c "Print :CFBundleExecutable" "$a/Info.plist"); echo "$a: $(lipo -archs "$a/$x") $(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$a/Info.plist")"; }; done
echo "=== done $(date +%T)"
