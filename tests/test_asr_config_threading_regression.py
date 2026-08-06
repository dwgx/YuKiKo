"""`config.media.asr` 必须真的传到转写函数，否则配置是摆设。

ASR 车道把 faster-whisper 接进了 utils/media.py，函数签名收
model_size / device / compute_type / timeout。但调用点
（core/agent_tools_media.py 的 _try_voice_candidates）原来是
`transcribe_audio_enhanced(wav_path, language="zh")` —— 一个规格都没传。
后果：config.yml / WebUI 里改 media.asr.* 完全不生效，实际只有
YUKIKO_ASR_* 环境变量说得上话，而业主是在配置里调的。

同时锁住 enable=false 的语义：关掉 ASR 时不能去加载模型，
也不能谎称"这段语音像是没录上声音"（那是上一轮刚修掉的伪装）。
"""

from __future__ import annotations

import pathlib
import unittest

import yaml

from core.agent_tools_media import _resolve_asr_spec
from core.config_templates import deep_merge_dict, load_config_template


class AsrSpecResolutionTests(unittest.TestCase):
    def test_reads_runtime_config_from_context(self) -> None:
        spec = _resolve_asr_spec(
            {
                "config": {
                    "media": {
                        "asr": {
                            "enable": True,
                            "model_size": "base",
                            "device": "cuda",
                            "compute_type": "float16",
                            "timeout_seconds": 120,
                        }
                    }
                }
            }
        )
        self.assertEqual(spec["model_size"], "base")
        self.assertEqual(spec["device"], "cuda")
        self.assertEqual(spec["compute_type"], "float16")
        self.assertEqual(spec["timeout"], 120.0)
        self.assertTrue(spec["enable"])

    def test_missing_config_yields_only_the_enable_flag(self) -> None:
        """没配置时不要编造规格 —— 让 utils/media.py 用它自己的兜底常量。"""

        self.assertEqual(_resolve_asr_spec({}), {"enable": True})
        self.assertEqual(_resolve_asr_spec({"config": {}}), {"enable": True})
        self.assertEqual(_resolve_asr_spec({"config": {"media": {}}}), {"enable": True})

    def test_disable_flag_is_carried(self) -> None:
        spec = _resolve_asr_spec({"config": {"media": {"asr": {"enable": False}}}})
        self.assertFalse(spec["enable"])

    def test_dirty_values_degrade_instead_of_raising(self) -> None:
        spec = _resolve_asr_spec(
            {
                "config": {
                    "media": {
                        "asr": {
                            "model_size": "   ",
                            "device": None,
                            "timeout_seconds": "abc",
                        }
                    }
                }
            }
        )
        self.assertNotIn("model_size", spec)
        self.assertNotIn("device", spec)
        self.assertNotIn("timeout", spec)

    def test_non_dict_config_is_tolerated(self) -> None:
        for bad in ("nope", 42, [], None):
            with self.subTest(repr(bad)):
                self.assertEqual(_resolve_asr_spec({"config": bad}), {"enable": True})

    def test_real_merged_config_produces_a_usable_spec(self) -> None:
        """用模板 + config.yml 的真实合并结果验证，而不是手造的 dict。"""

        raw = yaml.safe_load(
            pathlib.Path("config/config.yml").read_text(encoding="utf-8")
        ) or {}
        merged = deep_merge_dict(dict(load_config_template()), raw)
        spec = _resolve_asr_spec({"config": merged})
        self.assertTrue(spec["enable"])
        self.assertTrue(spec["model_size"], "model_size 必须有值，否则配置没落盘")
        self.assertTrue(spec["device"])
        self.assertTrue(spec["compute_type"])
        self.assertGreater(spec["timeout"], 0)

    def test_spec_keys_match_the_transcribe_signature(self) -> None:
        """键名要能直接 ** 展开进 transcribe_audio_enhanced，拼错了就静默失效。"""

        import inspect

        from utils.media import transcribe_audio_enhanced

        params = set(inspect.signature(transcribe_audio_enhanced).parameters)
        spec = _resolve_asr_spec(
            {
                "config": {
                    "media": {
                        "asr": {
                            "model_size": "small",
                            "device": "cpu",
                            "compute_type": "int8",
                            "timeout_seconds": 90,
                        }
                    }
                }
            }
        )
        spec.pop("enable", None)
        unknown = sorted(k for k in spec if k not in params)
        self.assertEqual(
            unknown, [], f"这些键 transcribe_audio_enhanced 不认，会直接 TypeError: {unknown}"
        )


class AsrTruthSourceTests(unittest.TestCase):
    def test_media_asr_present_in_both_truth_sources(self) -> None:
        from core.config_templates import _built_in_config_defaults

        template = yaml.safe_load(
            pathlib.Path("config/templates/master.template.yml").read_text(encoding="utf-8")
        )["config"]
        builtin = _built_in_config_defaults()
        t_asr = (template.get("media") or {}).get("asr") or {}
        b_asr = (builtin.get("media") or {}).get("asr") or {}
        self.assertTrue(t_asr, "master.template.yml 缺 config.media.asr")
        self.assertTrue(b_asr, "_built_in_config_defaults() 缺 media.asr")
        self.assertEqual(set(t_asr), set(b_asr))
        mismatched = {k: (t_asr[k], b_asr[k]) for k in t_asr if t_asr[k] != b_asr[k]}
        self.assertEqual(mismatched, {}, f"值不一致: {mismatched}")

    def test_defaults_match_the_module_constants(self) -> None:
        """三处同值：模板 / 内置默认 / utils/media.py 的 _ASR_DEFAULT_*。"""

        import utils.media as media

        template = yaml.safe_load(
            pathlib.Path("config/templates/master.template.yml").read_text(encoding="utf-8")
        )["config"]
        asr = (template.get("media") or {}).get("asr") or {}
        self.assertEqual(asr.get("model_size"), media._ASR_DEFAULT_MODEL_SIZE)
        self.assertEqual(asr.get("device"), media._ASR_DEFAULT_DEVICE)
        self.assertEqual(asr.get("compute_type"), media._ASR_DEFAULT_COMPUTE_TYPE)


if __name__ == "__main__":
    unittest.main()
