from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import Plain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


@dataclass(frozen=True)
class ArbiterContext:
    message_id: int
    msg_time: int
    self_id: int


@dataclass
class GroupArbiterState:
    window_id: int = 0
    resolved: bool = False
    winner_self_id: int | None = None


class EmojiLikeArbiter:
    _DEFAULT_ARBITER_EMOJI_ID = 111
    _END_MARK_EMOJI_ID = 124

    def __init__(self, arbiter_emoji_id: int = _DEFAULT_ARBITER_EMOJI_ID):
        self._EMOJI_ID = arbiter_emoji_id
        self._EMOJI_TYPE = "1"
        self._WAIT_SEC = 1.0

        self._FEEDBACK_EMOJI_ID = self._END_MARK_EMOJI_ID
        self._FEEDBACK_EMOJI_TYPE = "1"
        self._FEEDBACK_WAIT_SEC = 0.7

        self._TIME_SLICE = 60

    async def compete(self, bot: Any, ctx: ArbiterContext) -> bool:
        mid = ctx.message_id

        # 若仲裁已结束（124 已被任何 bot 贴上），直接认输，不再参与
        if await self._has_feedback(bot, mid):
            return False

        # 若本 bot 自己尚未贴过仲裁表情，则贴上，表明参与本次仲裁
        if not await self._is_participant(bot, mid, ctx.self_id):
            try:
                await bot.set_msg_emoji_like(
                    message_id=mid,
                    emoji_id=self._EMOJI_ID,
                    emoji_type=self._EMOJI_TYPE,
                    set=True,
                )
            except Exception:
                return False

        # 等待其它 bot 也贴上仲裁表情，收集全部参与者
        await asyncio.sleep(self._WAIT_SEC)

        # 二次检查：如果等待期间已有人贴上结束标记，直接认输
        if await self._has_feedback(bot, mid):
            return False

        users = await self._fetch_users(bot, mid, self._EMOJI_ID, self._EMOJI_TYPE)
        if not users:
            return False

        order = self._decide_order(users, ctx.msg_time)
        if not order:
            return False

        # 仅有一个参与者：由自己直接胜出并贴结束标记
        if len(order) == 1:
            if order[0] != ctx.self_id:
                return False
            await self._stamp_end_mark(bot, mid)
            return True

        # 多个参与者：按既定顺序递补，由胜者贴上结束标记 124
        for candidate in order:
            # 再次检查是否已有结束标记
            if await self._has_feedback(bot, mid):
                return candidate == ctx.self_id

            if candidate == ctx.self_id:
                # 轮到自己作为候选胜者，贴上结束标记
                ok = await self._stamp_end_mark(bot, mid)
                if not ok:
                    return False
                # 贴上后等待，确认标记存在
                await asyncio.sleep(self._FEEDBACK_WAIT_SEC)
                if await self._has_feedback(bot, mid):
                    return True
                return False

            # 轮到其它候选者，等待其贴结束标记
            await asyncio.sleep(self._FEEDBACK_WAIT_SEC)
            if await self._has_feedback(bot, mid):
                return False

        return False

    async def _stamp_end_mark(self, bot: Any, message_id: int) -> bool:
        try:
            await bot.set_msg_emoji_like(
                message_id=message_id,
                emoji_id=self._FEEDBACK_EMOJI_ID,
                emoji_type=self._FEEDBACK_EMOJI_TYPE,
                set=True,
            )
            return True
        except Exception:
            return False

    async def _is_participant(
        self, bot: Any, message_id: int, self_id: int
    ) -> bool:
        users = await self._fetch_users(
            bot, message_id, self._EMOJI_ID, self._EMOJI_TYPE
        )
        return self_id in users

    async def _fetch_users(
        self,
        bot: Any,
        message_id: int,
        emoji_id: int,
        emoji_type: str,
    ) -> list[int]:
        try:
            resp = await bot.fetch_emoji_like(
                message_id=message_id,
                emojiId=str(emoji_id),
                emojiType=emoji_type,
            )
        except Exception:
            return []
        likes = (resp or {}).get("emojiLikesList") or []
        users: list[int] = []
        for item in likes:
            try:
                users.append(int(item["tinyId"]))
            except Exception:
                continue
        return users

    async def _has_feedback(self, bot: Any, message_id: int) -> bool:
        users = await self._fetch_users(
            bot,
            message_id,
            self._FEEDBACK_EMOJI_ID,
            self._FEEDBACK_EMOJI_TYPE,
        )
        return bool(users)

    def _decide_order(self, users: list[int], msg_time: int) -> list[int]:
        participants = sorted(set(users))
        if not participants:
            return []
        base = (msg_time // self._TIME_SLICE) % len(participants)
        return [
            participants[(base + i) % len(participants)]
            for i in range(len(participants))
        ]


class ArbiterPlugin(Star):
    _MANUAL_COMMANDS = ("/botpk", "botpk")

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.arbiter_emoji_id = config.get("arbiter_emoji_id", 111)
        self.debug = config.get("debug", False)
        try:
            window_minutes = int(config.get("window_minutes", 30))
        except (TypeError, ValueError):
            window_minutes = 30
        if window_minutes < 1:
            window_minutes = 30
        self._window_seconds = window_minutes * 60
        self.arbiter = EmojiLikeArbiter(arbiter_emoji_id=self.arbiter_emoji_id)
        self._states: dict[str, GroupArbiterState] = {}

        if self.debug:
            logger.info(
                f"[Arbiter] 插件初始化，仲裁表情ID: {self.arbiter_emoji_id}, "
                f"结束标记表情ID: {EmojiLikeArbiter._END_MARK_EMOJI_ID}, "
                f"窗口时长: {window_minutes} 分钟"
            )

    async def initialize(self):
        if self.debug:
            logger.info("[Arbiter] 插件已就绪")

    def _get_window_id(self) -> int:
        return int(time.time() // self._window_seconds)

    def _get_state(self, group_id: str) -> GroupArbiterState:
        current_window = self._get_window_id()
        state = self._states.get(group_id)
        if state is None:
            state = GroupArbiterState(window_id=current_window)
            self._states[group_id] = state
            if self.debug:
                logger.info(
                    f"[Arbiter] 群 {group_id} 初始化窗口 {current_window}"
                )
            return state
        if state.window_id != current_window:
            if self.debug:
                logger.info(
                    f"[Arbiter] 群 {group_id} 窗口切换: "
                    f"{state.window_id} -> {current_window}"
                )
            state.window_id = current_window
            state.resolved = False
            state.winner_self_id = None
        return state

    @staticmethod
    def _is_plain_text_message(event: AstrMessageEvent) -> bool:
        chain = event.get_messages()
        if not chain:
            return False
        for seg in chain:
            if not isinstance(seg, Plain):
                return False
        return True

    async def _run_compete(
        self,
        *,
        event: AiocqhttpMessageEvent,
        state: GroupArbiterState,
        message_id: int,
        msg_time: int,
        self_id: int,
        tag: str,
    ) -> None:
        ctx = ArbiterContext(
            message_id=message_id, msg_time=msg_time, self_id=self_id
        )
        is_win = await self.arbiter.compete(bot=event.bot, ctx=ctx)
        state.resolved = True
        if is_win:
            state.winner_self_id = self_id
            if self.debug:
                logger.info(
                    f"[Arbiter] {tag} 仲裁胜出，self_id={self_id}，"
                    f"已贴结束标记 {EmojiLikeArbiter._END_MARK_EMOJI_ID}"
                )
            return
        state.winner_self_id = -1
        if self.debug:
            logger.info(f"[Arbiter] {tag} 仲裁失败，self_id={self_id}，拦截")
        event.stop_event()

    async def _find_arbiter_emoji_target(
        self,
        *,
        bot: Any,
        group_id: str,
        window_start: int,
    ) -> tuple[int, int] | None:
        try:
            resp = await bot.api.call_action(
                "get_group_msg_history",
                group_id=str(group_id),
                count=10,
                reverse_order=False,
                disable_get_url=True,
                parse_mult_msg=False,
                quick_reply=False,
                reverseOrder=False,
            )
        except Exception as e:
            if self.debug:
                logger.info(f"[Arbiter] 拉取群历史失败: {e}")
            return None

        messages = None
        if isinstance(resp, dict):
            messages = resp.get("messages")
        if not messages:
            return None

        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            raw_time = msg.get("time")
            if isinstance(raw_time, (int, float)):
                msg_time_val = int(raw_time)
            else:
                continue
            if msg_time_val < window_start:
                continue
            msg_id_raw = msg.get("message_id")
            if not msg_id_raw:
                continue
            try:
                msg_id = int(msg_id_raw)
            except (TypeError, ValueError):
                continue
            try:
                like_resp = await bot.fetch_emoji_like(
                    message_id=msg_id,
                    emojiId=str(self.arbiter_emoji_id),
                    emojiType="1",
                )
            except Exception:
                continue
            likes = (like_resp or {}).get("emojiLikesList") or []
            if likes:
                return msg_id, msg_time_val
        return None

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def on_all_message(self, event: AstrMessageEvent):
        if not isinstance(event, AiocqhttpMessageEvent):
            return

        group_id = event.message_obj.group_id
        if not group_id:
            return

        group_key = str(group_id)
        state = self._get_state(group_key)

        try:
            my_self_id = int(event.message_obj.self_id)
        except (TypeError, ValueError, AttributeError):
            return

        message_str = event.message_str.strip()
        raw = event.message_obj.raw_message
        if not isinstance(raw, dict):
            return
        try:
            message_id = int(raw["message_id"])
            msg_time = int(raw["time"])
            self_id = int(raw["self_id"])
        except (KeyError, ValueError):
            return

        # 手动仲裁命令：无论当前窗口是否已 resolved，都立刻重新开始仲裁
        if message_str.lower() in self._MANUAL_COMMANDS:
            if self.debug:
                logger.info(
                    f"[Arbiter] 群 {group_key} 检测到手动仲裁命令 {message_str!r}，"
                    f"强制重置窗口状态并重新仲裁"
                )
            state.resolved = False
            state.winner_self_id = None
            await self._run_compete(
                event=event,
                state=state,
                message_id=message_id,
                msg_time=msg_time,
                self_id=self_id,
                tag="手动仲裁",
            )
            return

        if state.resolved:
            if state.winner_self_id == my_self_id:
                if self.debug:
                    logger.info(
                        f"[Arbiter] 群 {group_key} 本窗口胜者为自己，放行"
                    )
                return
            if self.debug:
                logger.info(
                    f"[Arbiter] 群 {group_key} 本窗口胜者为其他 bot，拦截"
                )
            event.stop_event()
            return

        window_start = state.window_id * self._window_seconds
        target = await self._find_arbiter_emoji_target(
            bot=event.bot,
            group_id=group_key,
            window_start=window_start,
        )
        if target is not None:
            target_msg_id, target_msg_time = target
            if self.debug:
                logger.info(
                    f"[Arbiter] 群 {group_key} 检测到仲裁表情，"
                    f"目标消息 {target_msg_id}，自动参与仲裁"
                )
            await self._run_compete(
                event=event,
                state=state,
                message_id=target_msg_id,
                msg_time=target_msg_time,
                self_id=self_id,
                tag="表情触发",
            )
            return

        if not message_str:
            return
        if not self._is_plain_text_message(event):
            if self.debug:
                logger.info(
                    f"[Arbiter] 群 {group_key} 消息非纯文本，暂不触发仲裁"
                )
            return

        if self.debug:
            logger.info(f"[Arbiter] 群 {group_key} 首条纯文本消息触发仲裁")

        await self._run_compete(
            event=event,
            state=state,
            message_id=message_id,
            msg_time=msg_time,
            self_id=self_id,
            tag="首条消息",
        )