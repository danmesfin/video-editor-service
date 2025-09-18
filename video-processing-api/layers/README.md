# FFmpeg Lambda Layer

Place your prebuilt `ffmpeg-layer.zip` here and set the Terraform variable `ffmpeg_layer_zip_path` to its path when applying, e.g.:

```
terraform apply -var="ffmpeg_layer_zip_path=./layers/ffmpeg-layer.zip"
```

Notes:
- The ZIP must follow Lambda Layer structure, for example:
  - `bin/ffmpeg` (executable)
  - Optionally `bin/ffprobe`
- On Lambda, layer contents are mounted at `/opt`. This module expects ffmpeg at one of: `/opt/bin/ffmpeg` or `/opt/ffmpeg`.
- You can build a static ffmpeg binary (e.g., with ffmpeg-static builds or custom build for Amazon Linux 2023).
