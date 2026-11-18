#!/bin/sh
# Download the models that are too big for the repo (YuNet is small and
# committed). Run from anywhere: vision/tools/get_models.sh
set -e
cd "$(dirname "$0")/../host/models"
get() {   # file url sha256
    if [ -f "$1" ] && echo "$3  $1" | sha256sum -c --status; then
        echo "have $1"; return
    fi
    curl -fL -o "$1.part" "$2"
    echo "$3  $1.part" | sha256sum -c --status || { echo "bad checksum: $1" >&2; rm -f "$1.part"; exit 1; }
    mv "$1.part" "$1"
    echo "got $1"
}
get face_recognition_sface_2021dec.onnx \
    https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx \
    0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79
