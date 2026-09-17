import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import usage_float as usage_float_module

from usage_float import (
    BG_OPACITY_DEFAULT,
    DEFAULT_PROVIDERS,
    MonitorInfo,
    TEXT_OPACITY_DEFAULT,
    WALLPAPER_RANDOM_MODE_DIRECTORY,
    WALLPAPER_RANDOM_MODE_GLOBAL,
    _DoubleCtrlDetector,
    _PendingFileDrops,
    _apply_layer_opacities,
    _build_mpv_wallpaper_command,
    _build_wallpaper_playback_order,
    _with_preferred_wallpaper_first,
    _choose_panel_monitor,
    _clamp_opacity,
    _collect_wallpaper_media,
    _collect_wallpaper_media_folders,
    _compact_row_height,
    _commit_wallpaper_settings_before_close,
    _dropped_wallpaper_videos,
    _enabled_wallpaper_folders,
    _effective_card_height,
    _format_refresh_control,
    _install_windows_file_drop,
    _monitor_label,
    _merge_wallpaper_weight_values,
    _normalize_wallpaper_folders,
    _normalize_wallpaper_disabled_folders,
    _other_shortcut_key_down,
    _panel_grid_shape,
    _panel_metrics,
    _point_in_box,
    _randomize_wallpaper_session,
    _report_tk_callback_exception,
    _tk_geometry,
    _wallpaper_path_key,
    consume_codex_reset_card,
    fetch_codex_reset_cards,
    format_remaining,
    iter_display_rows,
    ProviderUsage,
    UsageWindow,
)


class WallpaperBackendTests(unittest.TestCase):
    class FixedRandom:
        def __init__(self, first_name: str, percent: float = 42.5) -> None:
            self.first_name = first_name
            self.percent = percent

        def shuffle(self, values: list[Path]) -> None:
            values.sort(key=lambda path: path.name != self.first_name)

        def uniform(self, _start: float, _end: float) -> float:
            return self.percent

    def test_media_collection_is_recursive_stable_and_filtered(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "nested").mkdir()
            (root / "b.MP4").write_bytes(b"")
            (root / "nested" / "a.png").write_bytes(b"")
            (root / "ignore.txt").write_text("no", encoding="utf-8")

            files = _collect_wallpaper_media(root)

        self.assertEqual([path.name for path in files], ["b.MP4", "a.png"])

    def test_folder_list_migrates_legacy_value_and_deduplicates(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            migrated = _normalize_wallpaper_folders([], root)
            deduplicated = _normalize_wallpaper_folders([root, str(root)])

        self.assertEqual(migrated, [str(root.resolve())])
        self.assertEqual(deduplicated, [str(root.resolve())])

    def test_load_config_migrates_the_saved_single_folder(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            legacy_folder = Path(temp_dir) / "media"
            config_path.write_text(
                json.dumps({"wallpaper_folder": str(legacy_folder)}),
                encoding="utf-8",
            )
            with patch.object(usage_float_module, "CONFIG_PATH", config_path):
                config = usage_float_module.load_config()

        self.assertEqual(config["wallpaper_folders"], [str(legacy_folder.resolve())])
        self.assertEqual(config["wallpaper_folder"], str(legacy_folder.resolve()))
        self.assertEqual(config["wallpaper_disabled_folders"], [])

    def test_disabled_folders_are_limited_to_saved_roots_and_keep_root_order(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "first"
            second = root / "second"
            missing = root / "removed"
            folders = [str(first), str(second)]

            disabled = _normalize_wallpaper_disabled_folders(
                [str(second), str(missing), str(second)],
                folders,
            )

        self.assertEqual(disabled, [str(second.resolve())])

    def test_enabled_folders_exclude_disabled_roots_without_deleting_them(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "first"
            second = root / "second"
            folders = [str(first), str(second)]

            enabled = _enabled_wallpaper_folders(folders, [str(first)])

        self.assertEqual(enabled, [str(second.resolve())])
        self.assertEqual(
            _normalize_wallpaper_folders(folders),
            [str(first.resolve()), str(second.resolve())],
        )

    def test_multiple_roots_merge_media_without_overlap_duplicates(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            nested = root / "nested"
            nested.mkdir()
            (root / "root.jpg").write_bytes(b"")
            (nested / "nested.mp4").write_bytes(b"")

            files = _collect_wallpaper_media_folders([str(root), str(nested)])

        self.assertEqual([path.name for path in files], ["nested.mp4", "root.jpg"])

    def test_preferred_drop_stays_first_without_reshuffling_the_queue(self) -> None:
        current = [Path("queue") / "a.mp4", Path("queue") / "b.mp4"]
        dropped = Path("other") / "dragged.mp4"

        kept, start = _build_wallpaper_playback_order(
            current,
            preferred_first=dropped,
            keep_order=True,
            rng=self.FixedRandom("b.mp4"),
        )
        reshuffled, _reshuffled_start = _build_wallpaper_playback_order(
            [*current, dropped],
            preferred_first=dropped,
            keep_order=False,
            rng=self.FixedRandom("b.mp4"),
        )

        self.assertIsNone(start)
        self.assertEqual(kept, [dropped, *current])
        self.assertEqual(kept, _with_preferred_wallpaper_first(current, dropped))
        self.assertEqual(reshuffled[0], dropped)
        self.assertEqual(reshuffled[1].name, "b.mp4")

    def test_dropped_video_filter_accepts_supported_files_only(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "opening.MP4"
            image = root / "still.jpg"
            video.write_bytes(b"")
            image.write_bytes(b"")

            videos = _dropped_wallpaper_videos(
                [str(video), str(image), str(video), str(root / "missing.mkv")]
            )

        self.assertEqual(videos, [video.resolve()])

    def test_pending_file_drops_are_drained_without_raising_into_tk(self) -> None:
        delivered: list[list[str]] = []
        errors: list[str] = []
        queue = _PendingFileDrops(delivered.append)
        queue.push(["opening.mp4", "extra.mkv"])
        queue.push([])
        queue.drain()
        self.assertEqual(delivered, [["opening.mp4", "extra.mkv"]])

        def boom(_files: list[str]) -> None:
            raise RuntimeError("boom")

        failing = _PendingFileDrops(boom)
        with patch.object(usage_float_module, "_log_exception", lambda _prefix="error": errors.append(_prefix)):
            failing.push(["bad.mp4"])
            failing.drain()

        self.assertEqual(errors, ["file drop"])
        self.assertEqual(delivered, [["opening.mp4", "extra.mkv"]])

    def test_ole_drop_target_registers_on_a_real_tk_window(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows only")
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        cleanup = None
        try:
            root.update_idletasks()
            cleanup = _install_windows_file_drop(root, lambda _files: None)
            self.assertTrue(getattr(cleanup, "_registered", False))
        finally:
            if cleanup is not None:
                cleanup()
            root.destroy()

    def test_drop_install_failure_returns_a_safe_cleanup(self) -> None:
        class BrokenWidget:
            def update_idletasks(self) -> None:
                raise RuntimeError("no native window")

            def winfo_id(self) -> int:
                return 0

        with patch.object(usage_float_module, "_log_exception"):
            cleanup = _install_windows_file_drop(BrokenWidget(), lambda _files: None)

        self.assertTrue(callable(cleanup))
        cleanup()

    def test_tk_callback_exception_is_logged_without_reraise(self) -> None:
        with TemporaryDirectory() as temp_dir:
            log_parent = Path(temp_dir)
            with patch.object(usage_float_module, "CONFIG_PATH", log_parent / "config.json"):
                try:
                    raise RuntimeError("callback exploded")
                except RuntimeError:
                    exc, val, tb = sys.exc_info()
                    _report_tk_callback_exception(exc, val, tb)

            logged = (log_parent / "error.log").read_text(encoding="utf-8")

        self.assertIn("tk callback", logged)
        self.assertIn("callback exploded", logged)

    def test_directory_weight_controls_the_first_session_item(self) -> None:
        files = [Path("low") / "still.png", Path("high") / "opening.jpg"]
        weights = {
            _wallpaper_path_key(Path("low")): 10,
            _wallpaper_path_key(Path("high")): 20,
        }

        ordered, start_percent = _randomize_wallpaper_session(
            files,
            self.FixedRandom("still.png", percent=25),
            folder_weights=weights,
        )

        self.assertEqual(ordered[0].parent.name, "high")
        self.assertIsNone(start_percent)

    def test_pending_weight_text_is_committed_without_focus_change(self) -> None:
        folder = Path("media") / "long directory"
        key = _wallpaper_path_key(folder)

        committed = _merge_wallpaper_weight_values(
            {key: 10},
            {key: "37"},
        )
        invalid = _merge_wallpaper_weight_values(
            committed,
            {key: "not a number"},
        )

        self.assertEqual(committed[key], 37)
        self.assertEqual(invalid[key], 37)

    def test_close_commits_weights_before_other_settings_handlers(self) -> None:
        calls: list[str] = []

        _commit_wallpaper_settings_before_close(
            lambda: calls.append("weights"),
            lambda: calls.append("other"),
        )

        self.assertEqual(calls, ["weights", "other"])

    def test_single_directory_mode_keeps_the_session_in_the_selected_directory(self) -> None:
        files = [
            Path("low") / "a.png",
            Path("low") / "b.jpg",
            Path("high") / "c.png",
            Path("high") / "d.jpg",
        ]
        weights = {
            _wallpaper_path_key(Path("low")): 10,
            _wallpaper_path_key(Path("high")): 20,
        }

        ordered, _start = _randomize_wallpaper_session(
            files,
            self.FixedRandom("a.png", percent=25),
            folder_weights=weights,
            random_mode=WALLPAPER_RANDOM_MODE_DIRECTORY,
        )

        self.assertEqual(len(ordered), 2)
        self.assertEqual({path.parent.name for path in ordered}, {"high"})

    def test_global_mode_keeps_media_from_every_directory(self) -> None:
        files = [Path("one") / "a.png", Path("two") / "b.jpg"]

        ordered, _start = _randomize_wallpaper_session(
            files,
            self.FixedRandom("a.png", percent=5),
            folder_weights={},
            random_mode=WALLPAPER_RANDOM_MODE_GLOBAL,
        )

        self.assertEqual({path.parent.name for path in ordered}, {"one", "two"})

    def test_mpv_command_embeds_loops_and_uses_low_overhead_options(self) -> None:
        command = _build_mpv_wallpaper_command(
            Path(r"C:\tools\mpv.exe"),
            0x1_0000_0042,
            Path(r"C:\cache\wallpaper.m3u8"),
            image_seconds=12,
            first_file=Path(r"D:\media\opening.mp4"),
            start_percent=42.5,
            ipc_pipe=r"\\.\pipe\usage-float-test",
            paused=True,
        )

        self.assertIn("--wid=66", command)
        self.assertIn("--hwdec=auto-safe", command)
        self.assertIn("--profile=fast", command)
        self.assertIn("--mute=yes", command)
        self.assertNotIn("--no-audio", command)
        self.assertIn("--cache=yes", command)
        self.assertIn("--cache-secs=2", command)
        self.assertIn("--demuxer-readahead-secs=2", command)
        self.assertIn("--demuxer-max-bytes=16MiB", command)
        self.assertIn("--prefetch-playlist=yes", command)
        self.assertIn("--input-default-bindings=no", command)
        self.assertIn("--input-vo-keyboard=yes", command)
        self.assertIn("--input-cursor=yes", command)
        self.assertIn("--input-builtin-dragging=no", command)
        self.assertIn("--drag-and-drop=insert-next", command)
        self.assertTrue(any(item.startswith("--script=") for item in command))
        self.assertIn("--loop-playlist=inf", command)
        self.assertIn("--image-display-duration=12", command)
        self.assertIn("--pause=yes", command)
        self.assertIn(
            r"--input-ipc-server=\\.\pipe\usage-float-test",
            command,
        )
        self.assertIn("--start=42.50%", command)
        self.assertLess(command.index("--{"), command.index("--start=42.50%"))
        self.assertLess(command.index("--start=42.50%"), command.index("--}"))

    def test_session_randomizer_leaves_playback_progress_to_mpv_policy(self) -> None:
        files = [Path("still.png"), Path("clip.mp4"), Path("other.jpg")]

        video_order, video_start = _randomize_wallpaper_session(
            files,
            self.FixedRandom("clip.mp4"),
        )
        image_order, image_start = _randomize_wallpaper_session(
            files,
            self.FixedRandom("still.png"),
        )

        self.assertEqual(video_order[0].name, "clip.mp4")
        self.assertIsNone(video_start)
        self.assertEqual(image_order[0].name, "still.png")
        self.assertIsNone(image_start)

    def test_audio_can_be_disabled_for_wallpaper_sessions(self) -> None:
        command = _build_mpv_wallpaper_command(
            Path(r"C:\tools\mpv.exe"),
            42,
            None,
            first_file=Path(r"D:\media\silent.mp4"),
            audio_enabled=False,
        )

        self.assertIn("--no-audio", command)
        self.assertNotIn("--mute=yes", command)


class DoubleCtrlDetectorTests(unittest.TestCase):
    @staticmethod
    def tap(
        detector: _DoubleCtrlDetector,
        pressed_at: float,
        *,
        used: bool = False,
    ) -> bool:
        detector.update(True, False, pressed_at)
        if used:
            detector.update(True, True, pressed_at + 0.01)
        return detector.update(False, False, pressed_at + 0.05)

    def test_two_quick_clean_ctrl_taps_toggle(self) -> None:
        detector = _DoubleCtrlDetector(interval_seconds=0.45)

        self.assertFalse(self.tap(detector, 1.0))
        self.assertTrue(self.tap(detector, 1.25))

    def test_slow_or_modified_ctrl_taps_do_not_toggle(self) -> None:
        detector = _DoubleCtrlDetector(interval_seconds=0.45)

        self.assertFalse(self.tap(detector, 1.0))
        self.assertFalse(self.tap(detector, 1.8))
        self.assertFalse(self.tap(detector, 2.0, used=True))
        self.assertFalse(self.tap(detector, 2.2))
        self.assertTrue(self.tap(detector, 2.4))

    def test_sticky_ime_state_key_does_not_turn_ctrl_into_a_combo(self) -> None:
        def ime_only(vk: int) -> int:
            return 0x8000 if vk == 0x19 else 0

        def real_ctrl_c(vk: int) -> int:
            return 0x8000 if vk in (0x19, 0x43) else 0

        self.assertFalse(_other_shortcut_key_down(ime_only))
        self.assertTrue(_other_shortcut_key_down(real_ctrl_c))


class RemainingTimeTests(unittest.TestCase):
    def test_day_and_hour_segments_have_one_space(self) -> None:
        now = datetime(2026, 8, 24, tzinfo=timezone.utc)

        self.assertEqual(
            format_remaining((now + timedelta(days=3, hours=8)).isoformat(), now),
            "3d 8h",
        )
        self.assertEqual(
            format_remaining((now + timedelta(hours=3, minutes=31)).isoformat(), now),
            "3h 31m",
        )


class FakeWindow:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, float]] = []

    def attributes(self, name: str, value: float) -> None:
        self.calls.append((name, value))
        if self.fail:
            raise RuntimeError("layer update failed")


class OpacityTests(unittest.TestCase):
    def test_clamp_opacity_normalizes_invalid_and_out_of_range_values(self) -> None:
        self.assertEqual(_clamp_opacity(-1, BG_OPACITY_DEFAULT), 5)
        self.assertEqual(_clamp_opacity(140, BG_OPACITY_DEFAULT), 100)
        self.assertEqual(_clamp_opacity("42", BG_OPACITY_DEFAULT), 42)
        self.assertEqual(_clamp_opacity(None, BG_OPACITY_DEFAULT), BG_OPACITY_DEFAULT)

    def test_plate_and_text_receive_independent_opacity_values(self) -> None:
        text = FakeWindow()
        plate = FakeWindow()

        _apply_layer_opacities(text, plate, 20, 85)
        _apply_layer_opacities(text, plate, 60, 85)

        self.assertEqual(plate.calls, [("-alpha", 0.2), ("-alpha", 0.6)])
        self.assertEqual(text.calls, [("-alpha", 0.85), ("-alpha", 0.85)])

    def test_single_window_fallback_never_applies_plate_opacity_to_text(self) -> None:
        text = FakeWindow()

        _apply_layer_opacities(text, None, 15, 90)

        self.assertEqual(text.calls, [("-alpha", 0.9)])

    def test_plate_failure_does_not_block_text_preview_or_restack(self) -> None:
        text = FakeWindow()
        plate = FakeWindow(fail=True)
        restacks: list[bool] = []

        _apply_layer_opacities(
            text,
            plate,
            35,
            70,
            restack=lambda: restacks.append(True),
        )

        self.assertEqual(text.calls, [("-alpha", 0.7)])
        self.assertEqual(restacks, [True])

    def test_invalid_values_fall_back_per_layer(self) -> None:
        text = FakeWindow()
        plate = FakeWindow()

        _apply_layer_opacities(text, plate, object(), object())

        self.assertEqual(plate.calls, [("-alpha", BG_OPACITY_DEFAULT / 100)])
        self.assertEqual(text.calls, [("-alpha", TEXT_OPACITY_DEFAULT / 100)])


class RefreshControlTests(unittest.TestCase):
    def test_relative_time_and_refresh_icon_are_one_label(self) -> None:
        self.assertEqual(_format_refresh_control(100, False, now=142), "42 秒前  ↻")

    def test_refreshing_state_stays_in_the_same_control(self) -> None:
        self.assertEqual(_format_refresh_control(100, True, now=142), "更新中…  ↻")

    def test_never_refreshed_state_is_still_actionable(self) -> None:
        self.assertEqual(_format_refresh_control(None, False, now=142), "尚未更新  ↻")

    def test_transparent_padding_uses_the_same_click_target(self) -> None:
        hitbox = (4, 8, 84, 32)

        self.assertTrue(_point_in_box(4, 8, hitbox))
        self.assertTrue(_point_in_box(83, 31, hitbox))
        self.assertFalse(_point_in_box(84, 31, hitbox))
        self.assertFalse(_point_in_box(83, 32, hitbox))
        self.assertFalse(_point_in_box(20, 20, None))


class CompactLayoutTests(unittest.TestCase):
    def test_row_height_keeps_only_compact_safety_margin(self) -> None:
        self.assertEqual(_compact_row_height(16, 9), 18)
        self.assertEqual(_compact_row_height(21, 12), 23)

    def test_fixed_height_is_ignored_when_resizing_is_disabled(self) -> None:
        self.assertIsNone(_effective_card_height(192, False))
        self.assertEqual(_effective_card_height(192, True), 192)
        self.assertIsNone(_effective_card_height(None, True))


class AuxiliaryPanelTests(unittest.TestCase):
    @staticmethod
    def monitor(
        device: str,
        width: int,
        height: int,
        *,
        primary: bool = False,
        x: int = 0,
        y: int = 0,
        dpi: int = 96,
        monitor_id: str = "",
    ) -> MonitorInfo:
        return MonitorInfo(
            device=device,
            name="Test display",
            x=x,
            y=y,
            width=width,
            height=height,
            work_x=x,
            work_y=y,
            work_width=width,
            work_height=height,
            monitor_id=monitor_id,
            dpi_x=dpi,
            dpi_y=dpi,
            primary=primary,
        )

    def test_first_use_prefers_the_smallest_secondary_display(self) -> None:
        monitors = [
            self.monitor(r"\\.\DISPLAY1", 2560, 1440, primary=True),
            self.monitor(r"\\.\DISPLAY2", 2560, 1440, x=2560),
            self.monitor(r"\\.\DISPLAY3", 960, 640, y=1440),
        ]

        selected = _choose_panel_monitor(monitors, None)

        self.assertIsNotNone(selected)
        self.assertEqual(selected.device, r"\\.\DISPLAY3")

    def test_disconnected_persisted_panel_does_not_cover_the_primary(self) -> None:
        monitors = [self.monitor(r"\\.\DISPLAY1", 2560, 1440, primary=True)]

        self.assertIsNone(_choose_panel_monitor(monitors, r"\\.\DISPLAY3"))

    def test_hardware_identity_survives_windows_display_renumbering(self) -> None:
        panel_id = r"MONITOR\RTK2555\instance-2"
        monitors = [
            self.monitor(r"\\.\DISPLAY1", 2560, 1440, primary=True),
            self.monitor(
                r"\\.\DISPLAY2",
                960,
                640,
                x=-960,
                monitor_id=panel_id,
            ),
        ]

        selected = _choose_panel_monitor(
            monitors,
            r"\\.\DISPLAY3",
            r"MONITOR\RTK2555\old-instance",
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected.device, r"\\.\DISPLAY2")

    def test_panel_layout_uses_two_columns_on_the_960_by_640_display(self) -> None:
        self.assertEqual(_panel_grid_shape(5, 960, 640), (2, 3))
        self.assertEqual(_panel_grid_shape(5, 640, 960), (1, 5))
        metrics = _panel_metrics(960, 640, 5, 96)
        self.assertEqual((metrics.columns, metrics.rows), (2, 3))
        self.assertEqual(metrics.value_points, 48)
        self.assertEqual(metrics.title_points, 28)
        self.assertEqual(metrics.meta_points, 24)
        self.assertEqual(metrics.footer_points, 20)

    def test_monitor_label_and_negative_tk_geometry_are_stable(self) -> None:
        monitor = self.monitor(r"\\.\DISPLAY3", 960, 640, x=-960, dpi=144)

        self.assertEqual(_monitor_label(monitor), "显示器 3 · 960×640 · 150%")
        self.assertEqual(_tk_geometry(960, 640, -960, 0), "960x640-960+0")


class CodexResetCardTests(unittest.TestCase):
    LIST_URL = usage_float_module.CODEX_RESET_CARDS_URL
    CONSUME_URL = usage_float_module.CODEX_RESET_CONSUME_URL

    @staticmethod
    def iso(days: float) -> str:
        return (
            datetime.now(timezone.utc) + timedelta(days=days)
        ).isoformat().replace("+00:00", "Z")

    def test_cards_are_sorted_by_usability_then_soonest_expiry(self) -> None:
        payload = {
            "credits": [
                {"id": "spent", "status": "redeemed", "expires_at": self.iso(1)},
                {"id": "late", "status": "available", "expires_at": self.iso(20)},
                {"id": "soon", "status": "available", "expires_at": self.iso(2)},
            ],
            "available_count": 2,
        }
        with patch.object(
            usage_float_module,
            "codex_authorized_json",
            return_value=(200, payload),
        ):
            cards, error = fetch_codex_reset_cards()

        self.assertIsNone(error)
        self.assertEqual([card.card_id for card in cards], ["soon", "late", "spent"])
        self.assertEqual([card.usable for card in cards], [True, True, False])

    def test_epoch_and_camel_case_payloads_are_both_accepted(self) -> None:
        granted = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        payload = {
            "credits": [
                {
                    "creditId": "camel",
                    "status": "AVAILABLE",
                    "resetType": "codex",
                    "grantedAt": granted.timestamp(),
                    "expiresAt": (granted + timedelta(days=30)).timestamp(),
                }
            ]
        }
        with patch.object(
            usage_float_module,
            "codex_authorized_json",
            return_value=(200, payload),
        ):
            cards, error = fetch_codex_reset_cards()

        self.assertIsNone(error)
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card.card_id, "camel")
        self.assertTrue(card.usable)
        self.assertEqual(card.reset_type, "codex")
        self.assertEqual(usage_float_module.parse_iso(card.granted_at), granted)

    def test_missing_credential_and_http_failures_report_distinct_errors(self) -> None:
        with patch.object(
            usage_float_module, "codex_authorized_json", return_value=(0, None)
        ):
            cards, error = fetch_codex_reset_cards()
        self.assertEqual(cards, [])
        self.assertEqual(error, "未找到可用的 Codex 登录凭证")

        with patch.object(
            usage_float_module,
            "codex_authorized_json",
            return_value=(403, {"detail": "not eligible"}),
        ):
            cards, error = fetch_codex_reset_cards()
        self.assertEqual(cards, [])
        self.assertIn("HTTP 403", error or "")
        self.assertIn("not eligible", error or "")

    def test_consume_posts_the_credit_id_with_a_dedupe_key(self) -> None:
        seen: dict[str, object] = {}

        def fake_call(url, *, home=None, method="GET", body=None):
            seen["url"] = url
            seen["home"] = home
            seen["method"] = method
            seen["body"] = body
            return 200, {"windows_reset": True}

        with patch.object(usage_float_module, "codex_authorized_json", fake_call):
            ok, message = consume_codex_reset_card("card-1", request_id="fixed")

        self.assertTrue(ok)
        self.assertIn("已使用", message)
        self.assertEqual(seen["url"], self.CONSUME_URL)
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["home"], usage_float_module.CODEX_HOME)
        self.assertEqual(
            seen["body"], {"credit_id": "card-1", "redeem_request_id": "fixed"}
        )

    def test_each_consume_call_generates_its_own_dedupe_key(self) -> None:
        keys: list[str] = []

        def fake_call(url, *, home=None, method="GET", body=None):
            keys.append((body or {}).get("redeem_request_id"))
            return 200, None

        with patch.object(usage_float_module, "codex_authorized_json", fake_call):
            consume_codex_reset_card("card-1")
            consume_codex_reset_card("card-1")

        self.assertEqual(len(keys), 2)
        self.assertTrue(all(keys))
        self.assertNotEqual(keys[0], keys[1])

    def test_consume_failure_surfaces_the_backend_message(self) -> None:
        with patch.object(
            usage_float_module,
            "codex_authorized_json",
            return_value=(409, {"error": {"message": "already redeemed"}}),
        ):
            ok, message = consume_codex_reset_card("card-1")

        self.assertFalse(ok)
        self.assertIn("already redeemed", message)

    def test_display_rows_carry_the_provider_id_that_gates_the_click(self) -> None:
        providers = [
            ProviderUsage(
                provider_id="codex",
                display_name="codex",
                windows=[UsageWindow(id="usage", label="codex", used_pct=68.0)],
            ),
            ProviderUsage(
                provider_id="grok",
                display_name="grok",
                available=False,
                error="更新失败",
            ),
        ]

        rows = iter_display_rows(providers)

        self.assertEqual([row.provider_id for row in rows], ["codex", "grok"])


class CodexMultiAccountTests(unittest.TestCase):
    """A second Codex account is a second CODEX_HOME, never a shared default."""

    def _account(self, provider_id: str, home: Path) -> object:
        return usage_float_module.CodexAccount(provider_id, provider_id, home)

    def test_usage_is_read_from_the_account_home_and_labelled_with_its_id(self) -> None:
        seen: dict[str, object] = {}

        def fake_call(url, *, home=None, method="GET", body=None):
            seen["home"] = home
            return 200, {
                "plan_type": "pro",
                "rate_limit": {"primary_window": {"used_percent": 83.0}},
            }

        account = self._account("codex-2", Path("/tmp/.codex-2"))
        with patch.object(usage_float_module, "codex_authorized_json", fake_call):
            with patch.object(usage_float_module, "cache_put_provider", lambda _p: None):
                usage = usage_float_module.fetch_codex_usage(account)

        self.assertEqual(seen["home"], Path("/tmp/.codex-2"))
        self.assertEqual(usage.provider_id, "codex-2")
        self.assertEqual(usage.display_name, "codex-2")
        self.assertAlmostEqual(usage.windows[0].used_pct, 83.0)

    def test_a_failed_second_account_does_not_borrow_the_first_ones_identity(self) -> None:
        account = self._account("codex-2", Path("/tmp/.codex-2"))
        with patch.object(
            usage_float_module, "codex_authorized_json", return_value=(0, None)
        ):
            usage = usage_float_module.fetch_codex_usage(account)

        self.assertEqual(usage.provider_id, "codex-2")
        self.assertFalse(usage.available)

    def test_a_provider_without_credentials_is_dropped_rather_than_shown_failed(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir:
            signed_in = Path(temp_dir) / "signed-in"
            signed_in.mkdir()
            (signed_in / "auth.json").write_text(
                json.dumps({"tokens": {"access_token": "t"}}), encoding="utf-8"
            )
            accounts = (
                usage_float_module.CodexAccount("codex", "codex", signed_in),
                usage_float_module.CodexAccount(
                    "codex-2", "codex-2", Path(temp_dir) / "missing"
                ),
            )
            with patch.object(usage_float_module, "CODEX_ACCOUNTS", accounts), patch.object(
                usage_float_module,
                "CODEX_ACCOUNT_BY_ID",
                {a.provider_id: a for a in accounts},
            ):
                self.assertTrue(usage_float_module.provider_available("codex"))
                self.assertFalse(usage_float_module.provider_available("codex-2"))
                self.assertEqual(
                    usage_float_module.active_providers(["codex", "codex-2"]),
                    ["codex"],
                )

    def test_codex_2_is_slotted_under_codex_not_appended_after_grok(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(
                json.dumps({"providers": ["claude", "codex", "grok"]}), encoding="utf-8"
            )
            with patch.object(usage_float_module, "CONFIG_PATH", config_path):
                config = usage_float_module.load_config()

        self.assertEqual(
            config["providers"],
            ["claude", "codex", "codex-2", "grok", "llmproxy"],
        )
        self.assertEqual(
            config["provider_order"],
            ["claude", "codex", "codex-2", "grok", "llmproxy"],
        )
        self.assertEqual(config["disabled_providers"], [])

    def test_redeeming_targets_the_clicked_account_home(self) -> None:
        seen: dict[str, object] = {}

        def fake_call(url, *, home=None, method="GET", body=None):
            seen["home"] = home
            return 200, None

        with patch.object(usage_float_module, "codex_authorized_json", fake_call):
            consume_codex_reset_card("card-1", home=Path("/tmp/.codex-2"))

        self.assertEqual(seen["home"], Path("/tmp/.codex-2"))


class LlmProxyUsageTests(unittest.TestCase):
    def test_openai_base_keeps_or_adds_v1(self) -> None:
        self.assertEqual(
            usage_float_module._llm_proxy_openai_base(
                "https://llm-proxy.example.com/v1"
            ),
            "https://llm-proxy.example.com/v1",
        )
        self.assertEqual(
            usage_float_module._llm_proxy_openai_base(
                "https://llm-proxy.example.com"
            ),
            "https://llm-proxy.example.com/v1",
        )
        self.assertEqual(usage_float_module._llm_proxy_openai_base(""), "")

    def test_spend_headers_are_read_case_insensitively(self) -> None:
        spend, budget, cost = usage_float_module.parse_llm_proxy_spend_headers(
            {
                "X-Litellm-Key-Spend": "10.5",
                "X-Litellm-Key-Max-Budget": "200",
                "X-Litellm-Response-Cost": "0.01",
            }
        )
        self.assertAlmostEqual(spend or 0, 10.5)
        self.assertAlmostEqual(budget or 0, 200)
        self.assertAlmostEqual(cost or 0, 0.01)

    def test_spend_is_shown_as_integer_dollars(self) -> None:
        self.assertEqual(usage_float_module.format_llm_proxy_spend(2.65, 200), "$3/200")
        self.assertEqual(usage_float_module.format_llm_proxy_spend(2.5, 200), "$3/200")
        self.assertEqual(usage_float_module.format_llm_proxy_spend(2.4, 200), "$2/200")
        self.assertEqual(usage_float_module.format_llm_proxy_spend(2.0, 200), "$2/200")
        self.assertEqual(usage_float_module.format_llm_proxy_spend(12.3), "$12")

    def test_stale_spend_is_the_current_request_cost(self) -> None:
        self.assertTrue(
            usage_float_module._spend_looks_like_this_request(2e-07, 2e-07)
        )
        self.assertFalse(
            usage_float_module._spend_looks_like_this_request(0.1047, 2e-07)
        )

    def test_liwork_key_makes_the_row_available(self) -> None:
        with TemporaryDirectory() as temp_dir:
            orca = Path(temp_dir) / "orca-data.json"
            orca.write_text(
                json.dumps(
                    {
                        "settings": {
                            "llmProxyApiKey": "sk-test-key",
                            "llmProxyEndpoint": "https://llm-proxy.example.com/v1",
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(usage_float_module, "LIWORK_ORCA_PATH", orca), patch.dict(
                "os.environ",
                {"LLM_PROXY_API_KEY": "", "LLM_PROXY_ENDPOINT": ""},
                clear=False,
            ), patch.object(usage_float_module, "CONFIG_PATH", Path(temp_dir) / "missing.json"):
                self.assertTrue(usage_float_module.provider_available("llmproxy"))
                key, base = usage_float_module.read_llm_proxy_credentials()
        self.assertEqual(key, "sk-test-key")
        self.assertEqual(base, "https://llm-proxy.example.com/v1")

    def test_missing_key_hides_the_row(self) -> None:
        with TemporaryDirectory() as temp_dir:
            with patch.object(
                usage_float_module,
                "LIWORK_ORCA_PATH",
                Path(temp_dir) / "missing.json",
            ), patch.object(
                usage_float_module, "CONFIG_PATH", Path(temp_dir) / "cfg.json"
            ), patch.dict(
                "os.environ",
                {"LLM_PROXY_API_KEY": "", "LLM_PROXY_ENDPOINT": ""},
                clear=False,
            ):
                self.assertFalse(usage_float_module.provider_available("llmproxy"))

    def test_fetch_retries_when_spend_equals_this_request(self) -> None:
        calls: list[int] = []

        def fake_probe(_base, _key, *, model, kind):
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                return 200, {
                    "x-litellm-key-spend": "0.0000002",
                    "x-litellm-key-max-budget": "200.0",
                    "x-litellm-response-cost": "0.0000002",
                }
            return 200, {
                "x-litellm-key-spend": "20.0",
                "x-litellm-key-max-budget": "200.0",
                "x-litellm-response-cost": "0.0000002",
            }

        with patch.object(
            usage_float_module,
            "read_llm_proxy_credentials",
            return_value=("sk-test", "https://llm-proxy.example.com/v1"),
        ), patch.object(
            usage_float_module, "_llm_proxy_probe", fake_probe
        ), patch.object(usage_float_module, "cache_put_provider", lambda _p: None):
            usage = usage_float_module.fetch_llmproxy_usage()

        self.assertEqual(len(calls), 2)
        self.assertTrue(usage.available)
        self.assertAlmostEqual(usage.windows[0].used_pct, 10.0)
        self.assertEqual(usage.summary, "$20/200")
        rows = iter_display_rows([usage])
        self.assertEqual(rows[0].title, "llmproxy")
        self.assertEqual(rows[0].summary, "$20/200")


class ProviderConfigTests(unittest.TestCase):
    def test_new_defaults_are_enabled_unless_already_disabled(self) -> None:
        order, enabled, disabled = usage_float_module.resolve_provider_config(
            ["claude", "codex", "grok"],
            [],
            ["claude", "codex", "grok"],
        )
        self.assertEqual(order, ["claude", "codex", "codex-2", "grok", "llmproxy"])
        self.assertEqual(enabled, ["claude", "codex", "codex-2", "grok", "llmproxy"])
        self.assertEqual(disabled, [])

    def test_disabled_providers_keep_order_and_stay_hidden(self) -> None:
        order, enabled, disabled = usage_float_module.resolve_provider_config(
            ["grok", "codex"],
            ["claude"],
            ["grok", "codex", "claude"],
        )
        self.assertEqual(order, ["grok", "codex", "codex-2", "claude", "llmproxy"])
        self.assertEqual(enabled, ["grok", "codex", "codex-2", "llmproxy"])
        self.assertEqual(disabled, ["claude"])

    def test_unchecking_every_row_stays_empty(self) -> None:
        order, enabled, disabled = usage_float_module.resolve_provider_config(
            [],
            list(DEFAULT_PROVIDERS),
            list(DEFAULT_PROVIDERS),
        )
        self.assertEqual(order, list(DEFAULT_PROVIDERS))
        self.assertEqual(enabled, [])
        self.assertEqual(disabled, list(DEFAULT_PROVIDERS))

    def test_move_provider_places_source_on_target_index(self) -> None:
        self.assertEqual(
            usage_float_module.move_provider(
                ["codex", "grok", "claude"], "codex", "claude"
            ),
            ["grok", "claude", "codex"],
        )
        self.assertEqual(
            usage_float_module.move_provider(
                ["codex", "grok", "claude"], "claude", "codex"
            ),
            ["claude", "codex", "grok"],
        )

    def test_load_config_preserves_explicit_disabled_list(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "providers": ["grok", "codex"],
                        "provider_order": ["grok", "codex", "claude"],
                        "disabled_providers": ["claude"],
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(usage_float_module, "CONFIG_PATH", config_path):
                config = usage_float_module.load_config()

        self.assertEqual(
            config["provider_order"],
            ["grok", "codex", "codex-2", "claude", "llmproxy"],
        )
        self.assertEqual(config["providers"], ["grok", "codex", "codex-2", "llmproxy"])
        self.assertEqual(config["disabled_providers"], ["claude"])

    def test_scan_providers_reports_login_state_in_saved_order(self) -> None:
        availability = {
            "codex": True,
            "codex-2": False,
            "grok": True,
            "claude": False,
            "llmproxy": True,
        }
        with patch.object(
            usage_float_module,
            "provider_available",
            side_effect=lambda pid: availability.get(pid, False),
        ):
            rows = usage_float_module.scan_providers(
                ["grok", "codex"],
                ["grok", "codex", "claude"],
            )

        self.assertEqual(
            [row["id"] for row in rows],
            ["grok", "codex", "codex-2", "claude", "llmproxy"],
        )
        self.assertEqual(rows[0]["label"], "Grok")
        self.assertTrue(rows[0]["available"])
        self.assertFalse(rows[3]["available"])


if __name__ == "__main__":
    unittest.main()
