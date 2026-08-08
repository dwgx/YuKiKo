"""E6a-end：checkpoint 自动恢复端到端接线回归测试。

锁四件事：
1. agent 兜底结果带出的 resume_checkpoint_id 会存入 engine 会话状态
   （get_last_resume_token），并经 EngineResponse.meta.resume_token 带出。
2. EngineMessage.resume_checkpoint_id 透传到 AgentLoop.run(resume_checkpoint_id=...)：
   第二次带 token 重跑同一会话从快照续跑，不重跑已完成步骤。
3. 不带 token 的新消息从零开始，不受会话旧 token 影响。
4. resume_checkpoint_id 指向不存在的 checkpoint 时安全回退从零开始。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.conftest import SequencedModelClient, make_engine, make_message


def _wrap_run_to_capture(engine, run_kwargs: list[dict]) -> None:
    """包一层真实 run()，捕获每次调用收到的 kwargs（含 resume_checkpoint_id）。"""
    real_run = engine.agent.run

    async def wrapped(ctx, **kwargs):
        run_kwargs.append(kwargs)
        return await real_run(ctx, **kwargs)

    engine.agent.run = wrapped  # type: ignore[method-assign]


class CheckpointResumeE2ERegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_result_stores_token_and_carries_in_meta(self) -> None:
        """模型响应耗尽 → agent 兜底 → token 进会话状态 + EngineResponse.meta。"""
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(
                responses=['{"tool":"web_search","args":{"query":"第一轮"}}'],
            )
            engine.agent.checkpoint_dir = Path(tmp)
            message = make_message(
                trace_id="e2e-fallback-1", message_id="m-e2e-1", text="帮我查一下"
            )

            response = await engine.handle_message(message)

            self.assertIsNotNone(response)
            self.assertTrue(str(response.reason).startswith("agent_fallback_"))
            # 响应 meta 带出恢复凭据（= 该次尝试的 trace_id）
            self.assertEqual(response.meta.get("resume_token"), "e2e-fallback-1")
            # 会话状态同样可查
            self.assertEqual(
                engine.get_last_resume_token(message.conversation_id), "e2e-fallback-1"
            )

    async def test_resume_token_message_resumes_from_snapshot(self) -> None:
        """带 resume_checkpoint_id 的消息：AgentLoop 收到 token 且从快照续跑。"""
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(
                responses=['{"tool":"web_search","args":{"query":"第一轮"}}'],
            )
            engine.agent.checkpoint_dir = Path(tmp)
            msg1 = make_message(
                trace_id="e2e-resume-1", message_id="m-e2e-r1", text="帮我查一下"
            )
            resp1 = await engine.handle_message(msg1)
            self.assertTrue(str(resp1.reason).startswith("agent_fallback_"))
            token = engine.get_last_resume_token(msg1.conversation_id)
            self.assertEqual(token, "e2e-resume-1")
            calls_before = list(engine._stub_registry.calls)

            # 续跑只给一条 final_answer：若从零重跑，第一步就会耗尽响应而报错
            new_client = SequencedModelClient(
                ['{"tool":"final_answer","args":{"text":"续跑完成"}}']
            )
            engine.agent.model_client = new_client
            run_kwargs: list[dict] = []
            _wrap_run_to_capture(engine, run_kwargs)
            msg2 = make_message(
                trace_id="e2e-resume-2",
                message_id="m-e2e-r2",
                text="帮我查一下",
                resume_checkpoint_id=token,
            )

            resp2 = await engine.handle_message(msg2)

            # AgentLoop 收到了 resume_checkpoint_id
            self.assertEqual(run_kwargs, [{"resume_checkpoint_id": "e2e-resume-1"}])
            self.assertEqual(resp2.reply_text, "续跑完成")
            # 已完成的 web_search 没有重跑
            self.assertEqual(engine._stub_registry.calls, calls_before)
            # 模型第一个请求带着恢复出的工具结果，而不是从零开始
            joined = json.dumps(new_client.messages_seen[0], ensure_ascii=False)
            self.assertIn("web_search 执行完成", joined)

    async def test_new_message_without_token_starts_fresh(self) -> None:
        """不带 token 的普通消息从零开始，且正常完成不写入会话 token。"""
        engine = make_engine(
            responses=[
                '{"tool":"web_search","args":{"query":"第二轮"}}',
                '{"tool":"final_answer","args":{"text":"完成"}}',
            ],
        )
        message = make_message(
            trace_id="e2e-fresh-1", message_id="m-e2e-f1", text="帮我查一下"
        )

        response = await engine.handle_message(message)

        self.assertEqual(response.reason, "agent_final_answer")
        self.assertEqual(response.reply_text, "完成")
        self.assertEqual(
            engine._stub_registry.calls, [("web_search", {"query": "第二轮"})]
        )
        # 正常完成不产生恢复凭据
        self.assertEqual(engine.get_last_resume_token(message.conversation_id), "")
        self.assertNotIn("resume_token", response.meta)

    async def test_ghost_resume_id_falls_back_to_fresh_start(self) -> None:
        """resume_checkpoint_id 指向不存在的 checkpoint：安全回退从零开始。"""
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(
                responses=[
                    '{"tool":"web_search","args":{"query":"第三轮"}}',
                    '{"tool":"final_answer","args":{"text":"完成"}}',
                ],
            )
            engine.agent.checkpoint_dir = Path(tmp)
            message = make_message(
                trace_id="e2e-ghost-1",
                message_id="m-e2e-g1",
                text="帮我查一下",
                resume_checkpoint_id="no-such-checkpoint",
            )

            response = await engine.handle_message(message)

            self.assertEqual(response.reason, "agent_final_answer")
            self.assertEqual(
                engine._stub_registry.calls, [("web_search", {"query": "第三轮"})]
            )


if __name__ == "__main__":
    unittest.main()
