"""Small checks for highlight selection and subtitle timing."""

import tempfile
import unittest
from pathlib import Path

from captions import Word, ass_timestamp, write_captions
from highlights import HighlightDetector, spoken_excitement
from reels import CAM_HEIGHT, GAME_HEIGHT, choose_layout, layout_crops, write_reel_caption


def line(user: str, text: str) -> str:
    return f":{user}!{user}@{user}.tmi.twitch.tv PRIVMSG #test :{text}"


class HighlightTests(unittest.TestCase):
    def test_one_user_spam_is_ignored(self) -> None:
        detector = HighlightDetector(10, 20, 3.0, 60)
        for index in range(3):
            for user in range(8):
                detector.add_line(line(f"user{user}", "hello"))
            self.assertIsNone(detector.close_bucket(index))
        for _ in range(80):
            detector.add_line(line("spammer", "LOL!!!"))
        self.assertIsNone(detector.close_bucket(3))

    def test_reaction_burst_is_selected(self) -> None:
        detector = HighlightDetector(10, 20, 3.0, 60)
        for index in range(3):
            for user in range(10):
                detector.add_line(line(f"user{user}", "hello"))
            detector.close_bucket(index)
        for user in range(35):
            detector.add_line(line(f"excited{user}", "NO WAY 😂"))
        highlight = detector.close_bucket(3)
        self.assertIsNotNone(highlight)
        self.assertEqual(highlight.second, 35)
        self.assertEqual(highlight.unique_chatters, 35)
        self.assertEqual(highlight.reactions, 35)

    def test_reaction_burst_without_large_volume_spike_is_selected(self) -> None:
        detector = HighlightDetector(10, 20, 3.0, 60)
        for index in range(3):
            for user in range(30):
                detector.add_message(f"baseline{index}_{user}", "hello")
            detector.close_bucket(index)
        for user in range(35):
            detector.add_message(f"excited{user}", "W")
        highlight = detector.close_bucket(3)
        self.assertIsNotNone(highlight)
        self.assertEqual(highlight.reason, "reaction_burst")

    def test_spoken_reaction_filters_audio_candidates(self) -> None:
        self.assertTrue(spoken_excitement("Oh my god, did that really happen?"))
        self.assertFalse(spoken_excitement("I am opening the settings menu."))


class CaptionTests(unittest.TestCase):
    def test_timed_word_highlights_and_editable_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "clip.mp4"
            srt, ass, txt = write_captions(
                [Word(1.0, 1.3, "No"), Word(1.3, 1.7, "way!"),
                 Word(2.3, 2.6, "Amazing")], base)
            self.assertIn("00:00:01,000 --> 00:00:01,700", srt.read_text(encoding="utf-8"))
            self.assertIn("{\\c&H00D7FF&}No", ass.read_text(encoding="utf-8"))
            self.assertIn("No way! Amazing", txt.read_text(encoding="utf-8"))
            self.assertEqual(ass_timestamp(61.234), "0:01:01.23")


class LayoutTests(unittest.TestCase):
    def test_static_caption_sits_on_split(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "caption.ass"
            write_reel_caption("SPEED THOUGHT HE WAS DONE", path)
            text = path.read_text(encoding="utf-8")
            self.assertIn(f"\\pos(540,{CAM_HEIGHT})", text)
            self.assertIn("SPEED THOUGHT", text)
            self.assertEqual(CAM_HEIGHT + GAME_HEIGHT, 1920)

    def test_precomposed_portrait_is_preserved(self) -> None:
        self.assertEqual(choose_layout(1080, 1920), "preserve")
        self.assertEqual(choose_layout(1920, 1080), "split")
        self.assertEqual(choose_layout(1920, 1080, "preserve"), "preserve")

    def test_gameplay_crop_is_vertical_and_webcam_uses_its_own_panel(self) -> None:
        game, cam = layout_crops(1920, 1080, (0.0, 0.55, 0.28, 0.34))
        self.assertEqual(game[1], 1080)
        self.assertLess(game[0], game[1])
        self.assertGreater(cam[0], 500)

    def test_game_focus_moves_crop_towards_right_side(self) -> None:
        left, _ = layout_crops(1920, 1080, (0.0, 0.55, 0.28, 0.34), 0.2)
        right, _ = layout_crops(1920, 1080, (0.0, 0.55, 0.28, 0.34), 0.8)
        self.assertGreater(right[2], left[2])


if __name__ == "__main__":
    unittest.main()
