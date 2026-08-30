from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_cloud_setup_provisions_and_checks_ffmpeg() -> None:
    script = (ROOT / "scripts" / "codex-cloud-setup.sh").read_text(encoding="utf-8")

    assert "command -v ffmpeg" in script
    assert "command -v ffprobe" in script
    assert "install_apt_package ffmpeg" in script
    assert "ffmpeg -hide_banner -version" in script
    assert "ffprobe -hide_banner -version" in script
