#!/bin/bash
# Builds AretyMessenger.apk without Gradle: resources -> classes -> dex -> aligned, signed APK.
set -euo pipefail
cd "$(dirname "$0")"
SDK=$HOME/Android/Sdk; BT=$SDK/build-tools/37.0.0; PLATFORM=$SDK/platforms/android-34/android.jar
KEYSTORE=$HOME/.config/arety/wa-line-release.jks; KEYPASS_FILE=$HOME/.config/arety/wa-line-release.pass
VERSION_CODE=${1:-1}

rm -rf build && mkdir -p build/classes build/gen
"$BT/aapt2" compile --dir res -o build/res.zip
"$BT/aapt2" link -o build/unsigned.apk -I "$PLATFORM" --manifest AndroidManifest.xml --java build/gen \
    --min-sdk-version 23 --target-sdk-version 34 --version-code "$VERSION_CODE" --version-name "1.$VERSION_CODE" build/res.zip
javac -nowarn -Xlint:-options --release 11 -cp "$PLATFORM" -d build/classes $(find src build/gen -name '*.java')
"$BT/d8" --lib "$PLATFORM" --min-api 23 --output build $(find build/classes -name '*.class')
(cd build && zip -q -j unsigned.apk classes.dex)
"$BT/zipalign" -f 4 build/unsigned.apk build/aligned.apk

if [ ! -f "$KEYSTORE" ]; then   # one signing key, kept so later versions install over this one
    (umask 077; head -c 24 /dev/urandom | base64 | tr -d '/+=' > "$KEYPASS_FILE")
    keytool -genkeypair -keystore "$KEYSTORE" -storepass "$(cat "$KEYPASS_FILE")" -alias arety -keyalg RSA -keysize 2048 \
        -validity 10000 -dname "CN=Arety Messenger" >/dev/null 2>&1
fi
"$BT/apksigner" sign --ks "$KEYSTORE" --ks-pass "file:$KEYPASS_FILE" --out ../AretyMessenger.apk build/aligned.apk
"$BT/apksigner" verify --print-certs ../AretyMessenger.apk | head -1
ls -la ../AretyMessenger.apk
