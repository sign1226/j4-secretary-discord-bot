import discord
from discord.ext import commands, tasks
import json
import os
import datetime
import asyncio
import logging
import shutil

# --- ログ設定 ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/app/data/bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger('j4-secretary')

# --- 設定 ---
DATA_DIR = "data"
BACKUP_DIR = "data/backup"
# ★ トークン（環境変数から読み込み、フォールバックで直書き）
TOKEN = os.environ.get("DISCORD_TOKEN", "")
if not TOKEN:
    logger.error("DISCORD_TOKEN environment variable is not set")
    raise SystemExit("DISCORD_TOKEN environment variable is not set")

DEFAULT_PLACES = ["西友", "ツルヤ", "業務スーパー", "アミカ", "ザ・ビッグ", "ラ・ムー", "ベイシア", "ダイソー", "セリア", "Amazon", "カインズ", "綿半"]

# OpenClaw の Bot ID
OPENCLAW_BOT_ID = 1481661263783268454

if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)
if not os.path.exists(BACKUP_DIR):
    os.makedirs(BACKUP_DIR)

def get_path(channel_id: int) -> str:
    return os.path.join(DATA_DIR, f"{channel_id}.json")

# データキャッシュ（channel_id -> data, timestamp）
_data_cache: dict[int, tuple[dict, float]] = {}
_backup_times: dict[int, float] = {}
_CACHE_TTL = 5.0  # キャッシュ有効期間（秒）

async def load_data(channel_id: int) -> dict:
    now = asyncio.get_event_loop().time()
    cached = _data_cache.get(channel_id)
    if cached and (now - cached[1]) < _CACHE_TTL:
        return cached[0]
    path = get_path(channel_id)
    default_structure = {
        "shopping": [], "schedule": [], "notes": [],
        "places": DEFAULT_PLACES.copy(), "history": {},
        "last_panel_id": None, "last_channel_id": channel_id,
        "active": True
    }
    if not os.path.exists(path):
        return default_structure
    try:
        data = await asyncio.to_thread(_load_data_sync, path)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"データ読み込み失敗 ({channel_id}): {e}")
        return default_structure
    for key, val in default_structure.items():
        if key not in data:
            data[key] = val
    _data_cache[channel_id] = (data, now)
    return data

def _load_data_sync(path: str) -> dict:
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

async def save_data(channel_id: int, data: dict) -> None:
    try:
        await asyncio.to_thread(_save_data_sync, get_path(channel_id), data)
        _data_cache[channel_id] = (data, asyncio.get_event_loop().time())
    except IOError as e:
        logger.error(f"データ保存失敗 ({channel_id}): {e}")

def _save_data_sync(path: str, data: dict) -> None:
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

async def auto_backup(channel_id: int) -> None:
    """データ保存時に自動バックアップ（同一チャンネルは60秒に1回まで）"""
    now = asyncio.get_event_loop().time()
    last = _backup_times.get(channel_id, 0)
    if now - last < 60.0:
        return
    _backup_times[channel_id] = now
    try:
        await asyncio.to_thread(_auto_backup_sync, channel_id)
    except Exception as e:
        logger.error(f"バックアップ失敗: {e}")

def _auto_backup_sync(channel_id: int) -> None:
    src = get_path(channel_id)
    if os.path.exists(src):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = os.path.join(BACKUP_DIR, f"{channel_id}_{ts}.json")
        shutil.copy2(src, dst)
        backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith(f"{channel_id}_")])
        for old in backups[:-10]:
            os.remove(os.path.join(BACKUP_DIR, old))

def create_panel_embed(data: dict) -> discord.Embed:
    embed = discord.Embed(title="✨ J4-Secretary", color=0x2b2d31)

    # 買い物リスト
    if shopping := data.get("shopping"):
        p_dict: dict[str, list[str]] = {}
        for it in shopping:
            p_dict.setdefault(it["place"], []).append(it["item"])
        lines = []
        for p in data["places"]:
            if p in p_dict:
                lines.append(f"**【{p}】**\n" + "\n".join(f"・{i}" for i in p_dict[p]))
        for p, items in p_dict.items():
            if p not in data["places"]:
                lines.append(f"**【{p}】**\n" + "\n".join(f"・{i}" for i in items))
        shop_text = "\n".join(lines) if lines else "なし"
    else:
        shop_text = "なし"
    embed.add_field(name="🛒 買い物リスト", value=shop_text, inline=False)

    # 予定
    schedule = data.get("schedule", [])
    t_list = "\n".join(
        f"**{it['display_dt']}** ｜ {'⏰' if it.get('snooze') else ''}{it['content']}"
        for it in schedule
    ) or "なし"
    embed.add_field(name="📅 予定", value=t_list, inline=False)

    # メモ
    notes = data.get("notes", [])
    n_list = "\n".join(f"・{n['content']}" for n in notes) or "なし"
    embed.add_field(name="📝 メモ", value=n_list, inline=False)

    return embed

async def flash_msg(channel: discord.TextChannel, text: str, seconds: int = 3, delete_after: int | None = None) -> None:
    try:
        await channel.send(f"@everyone {text}", delete_after=delete_after or seconds)
    except discord.HTTPException as e:
        logger.warning(f"flash_msg 失敗: {e}")

async def update_panel(channel: discord.TextChannel) -> None:
    data = await load_data(channel.id)
    view = ControlView(channel.id)
    if data.get("last_panel_id"):
        try:
            msg = await channel.fetch_message(data["last_panel_id"])
            await msg.delete()
        except (discord.NotFound, discord.HTTPException):
            pass
    new_msg = await channel.send(embed=create_panel_embed(data), view=view)
    data["last_panel_id"] = new_msg.id
    # save_data は update_panel の呼び出し元で行う（二重保存防止）

def parse_datetime_flexible(dt_str: str) -> str:
    """柔軟な日時パース: YYYY/MM/DD HH:MM, YYYY-MM-DD HH:MM, YYYY/MM/DD 等をサポート"""
    formats = [
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ]
    for fmt in formats:
        try:
            dt = datetime.datetime.strptime(dt_str.strip(), fmt)
            return dt.strftime("%Y/%m/%d %H:%M")
        except ValueError:
            continue
    raise ValueError(f"日時形式が不正です: {dt_str}\n対応形式: YYYY/MM/DD HH:MM, YYYY-MM-DD HH:MM, YYYY/MM/DD")

class J4Bot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix='!', intents=discord.Intents.all())

    async def setup_hook(self) -> None:
        self.remind_loop.start()
        self.daily_backup_loop.start()
        # Persistent Views の再登録（再起動後もボタンが動作するように）
        self.add_view(ControlView(0))  # channel_id はコールバック内で設定
        self.add_view(PlaceSettingsView(0, {"places": [], "history": {}}))
        self.add_view(HistorySettingsView(0, {"history": {}}))
        self.add_view(DeleteCategoryView(0, {"shopping": [], "schedule": [], "notes": []}))
        self.add_view(DelItemsView(0, {"shopping": [], "schedule": [], "notes": []}, "all"))
        self.add_view(EditCategoryView(0, {"shopping": [], "schedule": [], "notes": []}))
        self.add_view(EditItemsView(0, {"shopping": [], "schedule": [], "notes": []}, "shopping"))
        logger.info("Persistent Views 登録完了")

    async def on_ready(self) -> None:
        logger.info(f"Bot 起動: {self.user} (ID: {self.user.id})")

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandNotFound):
            return  # 未知のコマンドは無視
        logger.error("コマンドエラー: %s", error)

    async def on_message(self, message: discord.Message) -> None:
        # Bot 自身は無視
        if message.author.bot:
            # OpenClaw からの !setup は処理する
            if message.author.id == OPENCLAW_BOT_ID and message.content.startswith("!setup"):
                data = await load_data(message.channel.id)
                data["active"] = True
                await save_data(message.channel.id, data)
                await update_panel(message.channel)
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
            return

        # 通常のコマンド処理
        await self.process_commands(message)

        # コマンドの場合は追加ロジックを通さない
        if message.content.startswith('!'):
            return

        # --- 通常のテキストによる買い物追加ロジック ---
        data = await load_data(message.channel.id)
        if not data.get("active", True):
            return

        parts = message.content.split()
        if not parts:
            return

        if len(parts) >= 2:
            place, item = parts[0], " ".join(parts[1:])
            data["shopping"].append({"place": place, "item": item})
            data["history"][item] = place
        else:
            item = parts[0]
            place = data["history"].get(item, "不明")
            data["shopping"].append({"place": place, "item": item})

        await save_data(message.channel.id, data)
        await auto_backup(message.channel.id)

        try:
            await message.delete()
        except discord.HTTPException:
            pass

        await update_panel(message.channel)
        await flash_msg(message.channel, f"✅ **{item}** を追加しました（{place}）")

    @tasks.loop(seconds=30.0)
    async def remind_loop(self) -> None:
        if not os.path.exists(DATA_DIR):
            return
        now = datetime.datetime.now()
        for filename in os.listdir(DATA_DIR):
            if not filename.endswith(".json") or not filename.split(".")[0].isdigit():
                continue
            try:
                ch_id = int(filename.split(".")[0])
                data = await load_data(ch_id)
                ch = self.get_channel(ch_id)
                if not ch or not data.get("active", True) or not data.get("schedule"):
                    continue

                hit = False
                for it in data["schedule"]:
                    try:
                        trigger = datetime.datetime.strptime(it["trigger_dt"], "%Y/%m/%d %H:%M")
                    except ValueError:
                        continue
                    if now >= trigger:
                        await ch.send(f"@everyone 🔔 **{it['content']}**の時間です！", view=SnoozeStopView(ch_id, it.get("id")))
                        if it.get("snooze"):
                            it["trigger_dt"] = (now + datetime.timedelta(minutes=5)).strftime("%Y/%m/%d %H:%M")
                        else:
                            it["_delete"] = True
                        hit = True

                if hit:
                    data["schedule"] = [it for it in data["schedule"] if not it.get("_delete")]
                    await save_data(ch_id, data)
                    await auto_backup(ch_id)
                    await update_panel(ch)
            except Exception as e:
                logger.error(f"remind_loop エラー ({filename}): {e}")

    @tasks.loop(hours=24)
    async def daily_backup_loop(self) -> None:
        """全データの日次バックアップ"""
        try:
            for filename in os.listdir(DATA_DIR):
                if not filename.endswith(".json") or not filename.split(".")[0].isdigit():
                    continue
                ch_id = filename.split(".")[0]
                await auto_backup(ch_id)
            logger.info("日次バックアップ完了")
        except Exception as e:
            logger.error(f"日次バックアップエラー: {e}")

class ControlView(discord.ui.View):
    def __init__(self, channel_id: int) -> None:
        super().__init__(timeout=None)
        self.channel_id: int = channel_id

    @discord.ui.button(label="買う", emoji="🛒", style=discord.ButtonStyle.primary, row=0, custom_id="panel:buy")
    async def buy(self, i, b):
        data = await load_data(i.channel.id)
        view = discord.ui.View()
        for p in data["places"][:20]:
            btn = discord.ui.Button(label=p, style=discord.ButtonStyle.secondary)
            btn.callback = (lambda x: lambda it: self.add_sh(it, x))(p)
            view.add_item(btn)
        btn_u = discord.ui.Button(label="場所不明", emoji="❓", style=discord.ButtonStyle.secondary)
        btn_u.callback = lambda it: self.add_sh(it, "不明")
        view.add_item(btn_u)
        btn_n = discord.ui.Button(label="新しい場所を追加", emoji="➕", style=discord.ButtonStyle.success)
        btn_n.callback = self.add_new_place_sh
        view.add_item(btn_n)
        view.add_item(QuitButton(i.channel.id))
        await i.response.edit_message(content="**🛒 どこで買いますか？**", view=view, embed=None)

    async def add_sh(self, interaction, place):
        modal = discord.ui.Modal(title=f"🛒 {place}に追加")
        item_in = discord.ui.TextInput(label="品名")
        modal.add_item(item_in)
        async def _sub(it):
            data = await load_data(it.channel.id)
            data["shopping"].append({"place": place, "item": item_in.value})
            data["history"][item_in.value] = place
            await save_data(it.channel.id, data)
            await auto_backup(it.channel.id)
            await it.response.defer()
            await update_panel(it.channel)
            await flash_msg(it.channel, f"✅ **{item_in.value}** を登録しました")
        modal.on_submit = _sub
        await interaction.response.send_modal(modal)

    async def add_new_place_sh(self, interaction):
        modal = discord.ui.Modal(title="➕ 新しい場所と品名を入力")
        p_in = discord.ui.TextInput(label="場所（店名）")
        i_in = discord.ui.TextInput(label="品名")
        modal.add_item(p_in)
        modal.add_item(i_in)
        async def _sub(it):
            data = await load_data(it.channel.id)
            p, item = p_in.value, i_in.value
            if p not in data["places"]:
                data["places"].append(p)
            data["shopping"].append({"place": p, "item": item})
            data["history"][item] = p
            await save_data(it.channel.id, data)
            await auto_backup(it.channel.id)
            await it.response.defer()
            await update_panel(it.channel)
            await flash_msg(it.channel, f"✅ **{item}** を **{p}** に新しく登録しました")
        modal.on_submit = _sub
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="予定", emoji="📅", style=discord.ButtonStyle.primary, row=0, custom_id="panel:sched")
    async def sched(self, i, b):
        modal = discord.ui.Modal(title="📅 予定の追加")
        dt = discord.ui.TextInput(label="日時", default=datetime.datetime.now().strftime("%Y/%m/%d %H:%M"))
        snz = discord.ui.TextInput(label="無限スヌーズ？(y/n)", default="y")
        ct = discord.ui.TextInput(label="内容")
        modal.add_item(dt)
        modal.add_item(snz)
        modal.add_item(ct)
        async def _sub(it):
            try:
                trigger_dt = parse_datetime_flexible(dt.value)
            except ValueError as e:
                return await it.response.send_message(str(e), ephemeral=True)
            data = await load_data(it.channel.id)
            data["schedule"].append({
                "id": str(datetime.datetime.now().timestamp()),
                "display_dt": trigger_dt[5:],
                "trigger_dt": trigger_dt,
                "content": ct.value,
                "snooze": (snz.value.lower() == 'y')
            })
            await save_data(it.channel.id, data)
            await auto_backup(it.channel.id)
            await it.response.defer()
            await update_panel(it.channel)
            await flash_msg(it.channel, f"📅 予定 **{ct.value}** をセットしました")
        modal.on_submit = _sub
        await i.response.send_modal(modal)

    @discord.ui.button(label="メモ", emoji="📝", style=discord.ButtonStyle.primary, row=0, custom_id="panel:note")
    async def note(self, i, b):
        modal = discord.ui.Modal(title="📝 メモの追加")
        ct = discord.ui.TextInput(label="内容", style=discord.TextStyle.paragraph)
        modal.add_item(ct)
        async def _sub(it):
            data = await load_data(i.channel.id)
            data["notes"].append({"content": ct.value})
            await save_data(it.channel.id, data)
            await auto_backup(it.channel.id)
            await it.response.defer()
            await update_panel(it.channel)
            await flash_msg(it.channel, "📝 メモを保存しました")
        modal.on_submit = _sub
        await i.response.send_modal(modal)

    @discord.ui.button(label="編集", emoji="✏️", style=discord.ButtonStyle.secondary, row=1, custom_id="panel:edit")
    async def edit_menu(self, i, b):
        data = await load_data(i.channel.id)
        total = len(data["shopping"]) + len(data["schedule"]) + len(data["notes"])
        if total == 0:
            return await i.response.send_message("編集する項目がありません", ephemeral=True)
        await i.response.edit_message(content="**✏️ 編集するカテゴリを選んでください**",
                                       view=EditCategoryView(i.channel.id, data), embed=None)

    async def edit_shop(self, i, idx):
        modal = discord.ui.Modal(title="🛒 買い物の編集")
        data = await load_data(i.channel.id)
        item = data["shopping"][idx]
        p_in = discord.ui.TextInput(label="場所", default=item["place"])
        i_in = discord.ui.TextInput(label="品名", default=item["item"])
        modal.add_item(p_in)
        modal.add_item(i_in)
        async def _sub(it):
            d = await load_data(it.channel.id)
            d["shopping"][idx] = {"place": p_in.value, "item": i_in.value}
            d["history"][i_in.value] = p_in.value
            await save_data(it.channel.id, d)
            await auto_backup(it.channel.id)
            await it.response.defer()
            await update_panel(it.channel)
            await flash_msg(it.channel, f"✏️ **{i_in.value}** に更新しました")
        modal.on_submit = _sub
        await i.response.send_modal(modal)

    async def edit_sched(self, i, idx):
        modal = discord.ui.Modal(title="📅 予定の編集")
        data = await load_data(it.channel.id)
        item = data["schedule"][idx]
        dt_in = discord.ui.TextInput(label="日時", default=item["trigger_dt"])
        snz_in = discord.ui.TextInput(label="スヌーズ(y/n)", default="y" if item.get("snooze") else "n")
        ct_in = discord.ui.TextInput(label="内容", default=item["content"])
        modal.add_item(dt_in)
        modal.add_item(snz_in)
        modal.add_item(ct_in)
        async def _sub(it):
            d = await load_data(it.channel.id)
            d["schedule"][idx].update({
                "display_dt": dt_in.value[5:],
                "trigger_dt": dt_in.value,
                "content": ct_in.value,
                "snooze": (snz_in.value.lower() == 'y')
            })
            await save_data(it.channel.id, d)
            await auto_backup(it.channel.id)
            await it.response.defer()
            await update_panel(it.channel)
            await flash_msg(it.channel, "✏️ 予定を更新しました")
        modal.on_submit = _sub
        await i.response.send_modal(modal)

    async def edit_note(self, i, idx):
        modal = discord.ui.Modal(title="📝 メモの編集")
        data = await load_data(it.channel.id)
        item = data["notes"][idx]
        ct_in = discord.ui.TextInput(label="内容", style=discord.TextStyle.paragraph, default=item["content"])
        modal.add_item(ct_in)
        async def _sub(it):
            d = await load_data(it.channel.id)
            d["notes"][idx] = {"content": ct_in.value}
            await save_data(it.channel.id, d)
            await auto_backup(it.channel.id)
            await it.response.defer()
            await update_panel(it.channel)
            await flash_msg(it.channel, "✏️ メモを更新しました")
        modal.on_submit = _sub
        await i.response.send_modal(modal)

    @discord.ui.button(label="完了/削除", emoji="✅", style=discord.ButtonStyle.success, row=1, custom_id="panel:del_menu")
    async def del_menu(self, i, b):
        data = await load_data(i.channel.id)
        total = len(data["shopping"]) + len(data["schedule"])
        if total == 0:
            return await i.response.send_message("削除する項目がありません", ephemeral=True)
        await i.response.edit_message(content="**✅ カテゴリを選んでください**",
                                       view=DeleteCategoryView(i.channel.id, data), embed=None)

    async def bulk(self, i, key):
        data = await load_data(i.channel.id)
        data[key] = []
        await save_data(i.channel.id, data)
        await auto_backup(i.channel.id)
        await i.response.defer()
        await update_panel(i.channel)
        await flash_msg(i.channel, "✨ 全て完了しました")

    @discord.ui.button(label="設定", emoji="⚙️", style=discord.ButtonStyle.secondary, row=1, custom_id="panel:settings")
    async def settings(self, i, b):
        view = discord.ui.View()
        btn_p = discord.ui.Button(label="場所の管理", emoji="📍")
        btn_p.callback = self.place_settings
        btn_h = discord.ui.Button(label="履歴の管理", emoji="📖")
        btn_h.callback = self.history_settings
        view.add_item(btn_p)
        view.add_item(btn_h)
        view.add_item(QuitButton(i.channel.id))
        await i.response.edit_message(content="**⚙️ 設定メニュー**", view=view, embed=None)

    async def place_settings(self, i):
        data = await load_data(i.channel.id)
        view = PlaceSettingsView(i.channel.id, data)
        await i.response.edit_message(content="**📍 場所の管理**", view=view, embed=None)

    async def history_settings(self, i):
        data = await load_data(i.channel.id)
        view = HistorySettingsView(i.channel.id, data)
        await i.response.edit_message(content="**📖 履歴の管理**", view=view, embed=None)

class PlaceSettingsView(discord.ui.View):
    """場所の管理（ページネーション対応）"""
    PER_PAGE = 25

    def __init__(self, channel_id: int, data: dict, page: int = 0) -> None:
        super().__init__(timeout=None)
        self.channel_id: int = channel_id
        self.data: dict = data
        self.selected_place: str | None = None
        self.page: int = page

        places = data.get("places", [])
        self.total_pages = max(1, (len(places) + self.PER_PAGE - 1) // self.PER_PAGE)
        self.page = min(self.page, self.total_pages - 1)

        p_start = self.page * self.PER_PAGE
        p_end = p_start + self.PER_PAGE
        page_places = places[p_start:p_end]

        if page_places:
            opts = [discord.SelectOption(label=p) for p in page_places]
            sel = discord.ui.Select(
                placeholder=f"場所を選択... (ページ {self.page + 1}/{self.total_pages})",
                options=opts,
                custom_id="place:select"
            )
            sel.callback = self.select_cb
            self.add_item(sel)
            self.add_item(QuitButton(self.channel_id))

    async def select_cb(self, i):
        self.selected_place = i.data['values'][0]
        await i.response.defer()

    @discord.ui.button(label="◀ 前へ", style=discord.ButtonStyle.secondary, row=1, custom_id="place:prev")
    async def prev_page(self, i, b):
        if self.page > 0:
            self.page -= 1
        await i.response.edit_message(view=PlaceSettingsView(i.channel.id, self.data, self.page))

    @discord.ui.button(label="次へ ▶", style=discord.ButtonStyle.secondary, row=1, custom_id="place:next")
    async def next_page(self, i, b):
        places = self.data.get("places", [])
        total_pages = max(1, (len(places) + self.PER_PAGE - 1) // self.PER_PAGE)
        if self.page < total_pages - 1:
            self.page += 1
        await i.response.edit_message(view=PlaceSettingsView(i.channel.id, self.data, self.page))

    @discord.ui.button(label="上へ", style=discord.ButtonStyle.secondary, row=2, custom_id="place:up")
    async def move_up(self, i, b):
        if not self.selected_place:
            return await i.response.send_message("場所を選択してください", ephemeral=True)
        ps = self.data["places"]
        idx = ps.index(self.selected_place)
        if idx > 0:
            ps[idx], ps[idx-1] = ps[idx-1], ps[idx]
            await save_data(i.channel.id, self.data)
            await i.response.edit_message(view=PlaceSettingsView(i.channel.id, self.data, self.page))
        else:
            await i.response.defer()

    @discord.ui.button(label="下へ", style=discord.ButtonStyle.secondary, row=2, custom_id="place:down")
    async def move_down(self, i, b):
        if not self.selected_place:
            return await i.response.send_message("場所を選択してください", ephemeral=True)
        ps = self.data["places"]
        idx = ps.index(self.selected_place)
        if idx < len(ps) - 1:
            ps[idx], ps[idx+1] = ps[idx+1], ps[idx]
            await save_data(i.channel.id, self.data)
            await i.response.edit_message(view=PlaceSettingsView(i.channel.id, self.data, self.page))
        else:
            await i.response.defer()

    @discord.ui.button(label="追加", style=discord.ButtonStyle.primary, row=3, custom_id="place:add")
    async def add_p(self, i, b):
        modal = discord.ui.Modal(title="場所の追加")
        name = discord.ui.TextInput(label="店名")

        async def _sub(it):
            if name.value not in self.data["places"]:
                self.data["places"].append(name.value)
                await save_data(it.channel.id, self.data)
                await it.response.edit_message(view=PlaceSettingsView(it.channel.id, self.data, self.page))

        modal.add_item(name)
        modal.on_submit = _sub
        await i.response.send_modal(modal)

    @discord.ui.button(label="削除", style=discord.ButtonStyle.danger, row=3, custom_id="place:del")
    async def del_p(self, i, b):
        if self.selected_place:
            self.data["places"].remove(self.selected_place)
            await save_data(i.channel.id, self.data)
            await i.response.edit_message(view=PlaceSettingsView(i.channel.id, self.data, self.page))
        else:
            await i.response.defer()

    # QuitButton added in __init__ below


class HistorySettingsView(discord.ui.View):
    """履歴の管理（ページネーション対応）"""
    PER_PAGE = 25  # discord.SelectOption の最大数

    def __init__(self, channel_id: int, data: dict, page: int = 0) -> None:
        super().__init__(timeout=None)
        self.channel_id: int = channel_id
        self.data: dict = data
        self.selected_item: str | None = None
        self.page: int = page

        h = data.get("history", {})
        self.total_pages = max(1, (len(h) + self.PER_PAGE - 1) // self.PER_PAGE)
        self.page = min(self.page, self.total_pages - 1)

        # 現在のページのアイテムを取得
        # list() がコルーチンを返す問題を回避するため手動構築
        h_items = []
        for k, v in h.items():
            h_items.append((k, v))
        page_start = self.page * self.PER_PAGE
        page_end = page_start + self.PER_PAGE
        page_items = h_items[page_start:page_end]

        opts = [discord.SelectOption(label=f"{k} -> {v}", value=k) for k, v in page_items]
        if opts:
            sel = discord.ui.Select(
                placeholder=f"履歴を選択... (ページ {self.page + 1}/{self.total_pages})",
                options=opts,
                custom_id="hist:select"
            )
            sel.callback = self.select_cb
            self.add_item(sel)
        self.add_item(QuitButton(self.channel_id))

    async def select_cb(self, i):
        self.selected_item = i.data['values'][0]
        await i.response.defer()

    @discord.ui.button(label="◀ 前へ", style=discord.ButtonStyle.secondary, row=1, custom_id="hist:prev")
    async def prev_page(self, i, b):
        if self.page > 0:
            self.page -= 1
        await i.response.edit_message(view=HistorySettingsView(i.channel.id, self.data, self.page))

    @discord.ui.button(label="次へ ▶", style=discord.ButtonStyle.secondary, row=1, custom_id="hist:next")
    async def next_page(self, i, b):
        h = self.data.get("history", {})
        total_pages = max(1, (len(h) + self.PER_PAGE - 1) // self.PER_PAGE)
        if self.page < total_pages - 1:
            self.page += 1
        await i.response.edit_message(view=HistorySettingsView(i.channel.id, self.data, self.page))

    @discord.ui.button(label="新規追加/編集", style=discord.ButtonStyle.primary, row=2, custom_id="hist:add")
    async def add_h(self, i, b):
        modal = discord.ui.Modal(title="履歴の登録")
        item_in = discord.ui.TextInput(label="品名", default=self.selected_item or "")
        place_in = discord.ui.TextInput(label="場所", default=self.data["history"].get(self.selected_item, "") if self.selected_item else "")
        async def _sub(it):
            self.data["history"][item_in.value] = place_in.value
            await save_data(it.channel.id, self.data)
            await it.response.edit_message(view=HistorySettingsView(it.channel.id, self.data, self.page))
        modal.on_submit = _sub
        modal.add_item(item_in)
        modal.add_item(place_in)
        await i.response.send_modal(modal)

    @discord.ui.button(label="削除", style=discord.ButtonStyle.danger, row=2, custom_id="hist:del")
    async def del_h(self, i, b):
        if self.selected_item:
            self.data["history"].pop(self.selected_item, None)
            await save_data(i.channel.id, self.data)
            await i.response.edit_message(view=HistorySettingsView(i.channel.id, self.data, self.page))
        else:
            await i.response.defer()

    # QuitButton added in __init__ below



class EditCategoryView(discord.ui.View):
    """編集カテゴリ選択"""
    def __init__(self, channel_id: int, data: dict) -> None:
        super().__init__(timeout=None)
        self.channel_id: int = channel_id
        self.data: dict = data
        if data["shopping"]:
            btn = discord.ui.Button(label=f"🛒 買い物 ({len(data['shopping'])})", style=discord.ButtonStyle.primary, row=0)
            btn.callback = lambda i: i.response.edit_message(
                content="**🛒 買い物を編集**",
                view=EditItemsView(i.channel.id, self.data, "shopping"),
                embed=None
            )
            self.add_item(btn)
        if data["schedule"]:
            btn = discord.ui.Button(label=f"📅 予定 ({len(data['schedule'])})", style=discord.ButtonStyle.primary, row=1)
            btn.callback = lambda i: i.response.edit_message(
                content="**📅 予定を編集**",
                view=EditItemsView(i.channel.id, self.data, "schedule"),
                embed=None
            )
            self.add_item(btn)
        if data["notes"]:
            btn = discord.ui.Button(label=f"📝 メモ ({len(data['notes'])})", style=discord.ButtonStyle.primary, row=2)
            btn.callback = lambda i: i.response.edit_message(
                content="**📝 メモを編集**",
                view=EditItemsView(i.channel.id, self.data, "notes"),
                embed=None
            )
            self.add_item(btn)
        self.add_item(QuitButton(self.channel_id))


class EditItemsView(discord.ui.View):
    """アイテム編集（ページネーション対応）"""
    PER_PAGE = 25

    def __init__(self, channel_id: int, data: dict, category: str, page: int = 0) -> None:
        super().__init__(timeout=None)
        self.channel_id: int = channel_id
        self.data: dict = data
        self.category: str = category
        self.page: int = page

        items = data[category]
        self.total_pages = max(1, (len(items) + self.PER_PAGE - 1) // self.PER_PAGE)
        self.page = min(self.page, self.total_pages - 1)

        p_start = self.page * self.PER_PAGE
        p_end = p_start + self.PER_PAGE
        page_items = items[p_start:p_end]

        if category == "shopping":
            opts = [discord.SelectOption(label=f"🛒 {it['item']} ({it['place']})", value=str(p_start + idx))
                    for idx, it in enumerate(page_items)]
        elif category == "schedule":
            opts = [discord.SelectOption(label=f"📅 {it['content']}", value=str(p_start + idx))
                    for idx, it in enumerate(page_items)]
            for o in opts:
                o.description = page_items[int(o.value) - p_start].get("display_dt", "")
        else:
            opts = [discord.SelectOption(label=f"📝 {it['content'][:30]}", value=str(p_start + idx))
                    for idx, it in enumerate(page_items)]

        sel = discord.ui.Select(
            placeholder=f"編集する項目... (ページ {self.page + 1}/{self.total_pages})",
            options=opts,
            custom_id="edititems:select"
        )
        sel.callback = self.select_cb
        self.add_item(sel)
        self.add_item(QuitButton(self.channel_id))

    async def select_cb(self, i):
        idx = int(i.data['values'][0])
        page_start = self.page * self.PER_PAGE
        actual_idx = page_start + (idx - page_start)  # same as idx
        if self.category == "shopping":
            await self._edit_shop(i, actual_idx)
        elif self.category == "schedule":
            await self._edit_sched(i, actual_idx)
        else:
            await self._edit_note(i, actual_idx)

    async def _edit_shop(self, i, idx):
        modal = discord.ui.Modal(title="🛒 買い物の編集")
        item_data = self.data["shopping"][idx]
        p_in = discord.ui.TextInput(label="場所", default=item_data["place"])
        i_in = discord.ui.TextInput(label="品名", default=item_data["item"])
        modal.add_item(p_in)
        modal.add_item(i_in)
        async def _sub(it):
            d = await load_data(i.channel.id)
            d["shopping"][idx] = {"place": p_in.value, "item": i_in.value}
            d["history"][i_in.value] = p_in.value
            await save_data(i.channel.id, d)
            await auto_backup(i.channel.id)
            await it.response.defer()
            await update_panel(it.channel)
            await flash_msg(it.channel, f"✏️ **{i_in.value}** に更新しました")
        modal.on_submit = _sub
        await i.response.send_modal(modal)

    async def _edit_sched(self, i, idx):
        modal = discord.ui.Modal(title="📅 予定の編集")
        item_data = self.data["schedule"][idx]
        dt_in = discord.ui.TextInput(label="日時", default=item_data["trigger_dt"])
        snz_in = discord.ui.TextInput(label="スヌーズ(y/n)", default="y" if item_data.get("snooze") else "n")
        ct_in = discord.ui.TextInput(label="内容", default=item_data["content"])
        modal.add_item(dt_in)
        modal.add_item(snz_in)
        modal.add_item(ct_in)
        async def _sub(it):
            d = await load_data(i.channel.id)
            d["schedule"][idx].update({
                "display_dt": dt_in.value[5:],
                "trigger_dt": dt_in.value,
                "content": ct_in.value,
                "snooze": (snz_in.value.lower() == 'y')
            })
            await save_data(i.channel.id, d)
            await auto_backup(i.channel.id)
            await it.response.defer()
            await update_panel(it.channel)
            await flash_msg(it.channel, "✏️ 予定を更新しました")
        modal.on_submit = _sub
        await i.response.send_modal(modal)

    async def _edit_note(self, i, idx):
        modal = discord.ui.Modal(title="📝 メモの編集")
        item_data = self.data["notes"][idx]
        ct_in = discord.ui.TextInput(label="内容", style=discord.TextStyle.paragraph, default=item_data["content"])
        modal.add_item(ct_in)
        async def _sub(it):
            d = await load_data(i.channel.id)
            d["notes"][idx] = {"content": ct_in.value}
            await save_data(i.channel.id, d)
            await auto_backup(i.channel.id)
            await it.response.defer()
            await update_panel(it.channel)
            await flash_msg(it.channel, "✏️ メモを更新しました")
        modal.on_submit = _sub
        await i.response.send_modal(modal)

    @discord.ui.button(label="◀ 前へ", style=discord.ButtonStyle.secondary, row=1, custom_id="edititems:prev")
    async def prev_page(self, i, b):
        if self.page > 0:
            self.page -= 1
        await i.response.edit_message(view=EditItemsView(i.channel.id, self.data, self.category, self.page))

    @discord.ui.button(label="次へ ▶", style=discord.ButtonStyle.secondary, row=1, custom_id="edititems:next")
    async def next_page(self, i, b):
        if self.page < self.total_pages - 1:
            self.page += 1
        await i.response.edit_message(view=EditItemsView(i.channel.id, self.data, self.category, self.page))


class DeleteCategoryView(discord.ui.View):
    """完了/削除カテゴリ選択"""
    def __init__(self, channel_id: int, data: dict) -> None:
        super().__init__(timeout=None)
        self.channel_id: int = channel_id
        self.data: dict = data
        # "All items" button - shows all items from all categories
        total = len(data["shopping"]) + len(data["schedule"]) + len(data["notes"])
        if total > 0:
            btn_all = discord.ui.Button(label=f"🗂️ 全項目を完了 ({total})", style=discord.ButtonStyle.danger, row=0, custom_id="delcat:all")
            btn_all.callback = lambda i: i.response.edit_message(
                content="**🗂️ 全項目を完了**",
                view=DelItemsView(i.channel.id, self.data, "all"),
                embed=None
            )
            self.add_item(btn_all)
        if data["shopping"]:
            btn = discord.ui.Button(label=f"🛒 買い物を完了/削除 ({len(data['shopping'])})", style=discord.ButtonStyle.primary, row=1, custom_id="delcat:shopping")
            btn.callback = lambda i: i.response.edit_message(
                content="**🛒 買い物を完了/削除**",
                view=DelItemsView(i.channel.id, self.data, "shopping"),
                embed=None
            )
            self.add_item(btn)
        if data["schedule"]:
            btn = discord.ui.Button(label=f"📅 予定を完了/削除 ({len(data['schedule'])})", style=discord.ButtonStyle.primary, row=2, custom_id="delcat:schedule")
            btn.callback = lambda i: i.response.edit_message(
                content="**📅 予定を完了/削除**",
                view=DelItemsView(i.channel.id, self.data, "schedule"),
                embed=None
            )
            self.add_item(btn)
        if data["notes"]:
            btn = discord.ui.Button(label=f"📝 メモを削除 ({len(data['notes'])})", style=discord.ButtonStyle.primary, row=3, custom_id="delcat:notes")
            btn.callback = lambda i: i.response.edit_message(
                content="**📝 メモを削除**",
                view=DelItemsView(i.channel.id, self.data, "notes"),
                embed=None
            )
            self.add_item(btn)
        self.add_item(QuitButton(self.channel_id))


class DelItemsView(discord.ui.View):
    """アイテム完了/削除（複数選択対応）"""
    PER_PAGE = 25

    def __init__(self, channel_id: int, data: dict, category: str, page: int = 0) -> None:
        super().__init__(timeout=None)
        self.channel_id: int = channel_id
        self.data: dict = data
        self.category: str = category
        self.page: int = page
        self.selected_values: list[str] = []

        if category == "all":
            # Merge all categories into one list
            items = []
            for it in data["shopping"]:
                items.append({"type": "shopping", "label": f"🛒 {it['item']} ({it['place']})", "_orig": it})
            for it in data["schedule"]:
                items.append({"type": "schedule", "label": f"📅 {it['content']}", "_orig": it, "extra": it.get("display_dt", "")})
            for it in data["notes"]:
                items.append({"type": "notes", "label": f"📝 {it['content'][:30]}", "_orig": it})
        else:
            items = data[category]
        self._items = items
        self.total_pages = max(1, (len(items) + self.PER_PAGE - 1) // self.PER_PAGE)
        self.page = min(self.page, self.total_pages - 1)

        p_start = self.page * self.PER_PAGE
        p_end = p_start + self.PER_PAGE
        page_items = items[p_start:p_end]

        if category == "shopping":
            opts = [discord.SelectOption(label=f"🛒 {it['item']} ({it['place']})", value=str(p_start + idx))
                    for idx, it in enumerate(page_items)]
        elif category == "schedule":
            opts = [discord.SelectOption(label=f"📅 {it['content']}", value=str(p_start + idx))
                    for idx, it in enumerate(page_items)]
            for o in opts:
                o.description = page_items[int(o.value) - p_start].get("display_dt", "")
        elif category == "all":
            opts = [discord.SelectOption(label=it["label"], value=str(p_start + idx))
                    for idx, it in enumerate(page_items)]
            for idx, o in enumerate(opts):
                if page_items[idx]["type"] == "schedule":
                    o.description = page_items[idx].get("extra", "")
        else:
            opts = [discord.SelectOption(label=f"📝 {it['content'][:30]}", value=str(p_start + idx))
                    for idx, it in enumerate(page_items)]

        sel = discord.ui.Select(
            placeholder=f"完了する項目を選んでください (複数可) — ページ {self.page + 1}/{self.total_pages}",
            options=opts,
            min_values=1,
            max_values=len(opts),
            custom_id="delitems:select"
        )
        sel.callback = self.select_cb
        self.add_item(sel)
        self.add_item(QuitButton(self.channel_id))

    async def select_cb(self, i):
        self.selected_values = i.data['values']
        await i.response.send_message(
            f"✅ **{len(self.selected_values)}件** を選択しました。「完了」ボタンを押して確定してください。",
            ephemeral=True,
            delete_after=5
        )

    @discord.ui.button(label="✅ 選択した項目を完了", style=discord.ButtonStyle.danger, row=1, custom_id="delitems:confirm")
    async def confirm_delete(self, i, b):
        if not self.selected_values:
            return await i.response.send_message("項目を先に選択してください", ephemeral=True)
        indices = sorted([int(v) for v in self.selected_values], reverse=True)
        if self.category == "all":
            # Remove items from their respective categories
            count = 0
            for idx in indices:
                if idx < len(self._items):
                    item = self._items[idx]
                    cat = item["type"]
                    orig = item["_orig"]
                    if orig in self.data[cat]:
                        self.data[cat].remove(orig)
                        count += 1
            done_text = f"✅ {count}件を完了しました" if count > 0 else "⚠️ 選択した項目は既に削除されています"
            await save_data(i.channel.id, self.data)
            await auto_backup(i.channel.id)
            await i.response.edit_message(content=None, embed=create_panel_embed(self.data), view=ControlView(i.channel.id))
            await flash_msg(i.channel, done_text, delete_after=5)
        else:
            count = 0
            for idx in indices:
                if idx < len(self.data[self.category]):
                    self.data[self.category].pop(idx)
                    count += 1
            await save_data(i.channel.id, self.data)
            await auto_backup(i.channel.id)
            remaining = len(self.data[self.category])
            done_text = f"✅ {count}件を完了しました" if count > 0 else "⚠️ 選択した項目は既に削除されています"
            if remaining == 0:
                await i.response.edit_message(content=None, embed=create_panel_embed(self.data), view=ControlView(i.channel.id))
            else:
                new_page = min(self.page, max(0, (remaining - 1) // self.PER_PAGE))
                self.page = new_page
                await i.response.edit_message(content=None, embed=create_panel_embed(self.data), view=DelItemsView(i.channel.id, self.data, self.category, self.page))
            await flash_msg(i.channel, done_text, delete_after=5)

    @discord.ui.button(label="◀ 前へ", style=discord.ButtonStyle.secondary, row=2, custom_id="delitems:prev")
    async def prev_page(self, i, b):
        if self.page > 0:
            self.page -= 1
        await i.response.edit_message(view=DelItemsView(i.channel.id, self.data, self.category, self.page))

    @discord.ui.button(label="次へ ▶", style=discord.ButtonStyle.secondary, row=2, custom_id="delitems:next")
    async def next_page(self, i, b):
        items = self.data[self.category]
        total_pages = max(1, (len(items) + self.PER_PAGE - 1) // self.PER_PAGE)
        if self.page < total_pages - 1:
            self.page += 1
        await i.response.edit_message(view=DelItemsView(i.channel.id, self.data, self.category, self.page))

class QuitButton(discord.ui.Button):
    def __init__(self, channel_id: int) -> None:
        super().__init__(label="やめる", style=discord.ButtonStyle.danger, row=4, custom_id="nav:quit")
        self.channel_id: int = channel_id

    async def callback(self, i):
        data = await load_data(self.channel_id)
        await i.response.edit_message(content=None, embed=create_panel_embed(data), view=ControlView(self.channel_id))

class DeleteByPlaceSelect(discord.ui.Select):
    def __init__(self, channel_id: int, places: list[str]) -> None:
        self.channel_id: int = channel_id
        opts = [discord.SelectOption(label=f"📍 {p}を全て完了", value=p) for p in places]
        super().__init__(placeholder="場所単位で完了...", options=opts)

    async def callback(self, i):
        data = await load_data(self.channel_id)
        data["shopping"] = [item for item in data["shopping"] if item["place"] != self.values[0]]
        await save_data(self.channel_id, data)
        await auto_backup(self.channel_id)
        await i.response.defer()
        await update_panel(i.channel)
        await flash_msg(i.channel, f"📍 {self.values[0]} のリストを整理しました")

class DeleteSelect(discord.ui.Select):
    def __init__(self, channel_id: int, data: dict) -> None:
        self.channel_id: int = channel_id
        opts = [discord.SelectOption(label=f"🛒 {it['item']}", value=f"s_{i}") for i, it in enumerate(data["shopping"])] + \
               [discord.SelectOption(label=f"📅 {it['content']}", value=f"t_{i}") for i, it in enumerate(data["schedule"])] + \
               [discord.SelectOption(label=f"📝 {it['content'][:20]}", value=f"n_{i}") for i, it in enumerate(data["notes"])]
        super().__init__(placeholder="個別に削除...", options=opts[:25])

    async def callback(self, i):
        data = await load_data(self.channel_id)
        prefix = self.values[0][0]
        key = {"s": "shopping", "t": "schedule", "n": "notes"}[prefix]
        data[key].pop(int(self.values[0][2:]))
        await save_data(self.channel_id, data)
        await auto_backup(self.channel_id)
        await i.response.defer()
        await update_panel(i.channel)
        await flash_msg(i.channel, "🗑️ 削除しました")

class SnoozeStopView(discord.ui.View):
    def __init__(self, channel_id: int, sid: str) -> None:
        super().__init__(timeout=None)
        self.channel_id: int = channel_id
        self.sid: str = sid

    @discord.ui.button(label="完了", style=discord.ButtonStyle.success, custom_id="snooze:done")
    async def stop(self, i, b):
        data = await load_data(i.channel.id)
        data["schedule"] = [it for it in data["schedule"] if it.get("id") != self.sid]
        await save_data(i.channel.id, data)
        await auto_backup(i.channel.id)
        await i.response.defer()
        await update_panel(i.channel)
        await flash_msg(i.channel, "✅ 完了しました")

bot = J4Bot()

# 標準の help コマンドを削除（独自 help を登録するため）
bot.remove_command('help')

@bot.command()
async def setup(ctx):
    """パネルを表示"""
    data = await load_data(ctx.channel.id)
    data["active"] = True
    await save_data(ctx.channel.id, data)
    await update_panel(ctx.channel)
    await ctx.message.delete()

@bot.command()
async def stop(ctx):
    """Secretary機能を停止"""
    data = await load_data(ctx.channel.id)
    if data.get("last_panel_id"):
        try:
            msg = await ctx.channel.fetch_message(data["last_panel_id"])
            await msg.delete()
        except (discord.NotFound, discord.HTTPException):
            pass
    data["last_panel_id"] = None
    data["active"] = False
    await save_data(ctx.channel.id, data)
    await ctx.send("✅ Secretary機能を停止しました。")

@bot.command()
async def help(ctx):
    """ヘルプを表示"""
    embed = discord.Embed(title="📖 J4-Secretary ヘルプ", color=0x2b2d31)
    embed.add_field(
        name="📌 コマンド",
        value=(
            "`!setup` — パネルを表示\n"
            "`!stop` — Secretary機能を停止\n"
            "`!help` — このヘルプを表示\n"
            "`!search <キーワード>` — 買い物履歴を検索\n"
            "`!list` — 登録済み場所一覧"
        ),
        inline=False
    )
    embed.add_field(
        name="🛒 買い物の追加",
        value="`場所 品名` と送信するだけで追加できます。\n例: `西友 牛乳`",
        inline=False
    )
    embed.add_field(
        name="💡 ヒント",
        value=(
            "• 品名だけ送信すると、履歴から場所を自動補完\n"
            "• パネルのボタンで編集・削除も可能\n"
            "• 予定はリマインド機能付き"
        ),
        inline=False
    )
    await ctx.send(embed=embed)

@bot.command()
async def search(ctx, *, keyword: str):
    """買い物履歴を検索"""
    data = await load_data(ctx.channel.id)
    history = data.get("history", {})
    results = {k: v for k, v in history.items() if keyword in k}
    if not results:
        return await ctx.send(f"🔍 `{keyword}` に一致する履歴はありません。")
    lines = [f"・{k} → {v}" for k, v in list(results.items())[:20]]
    embed = discord.Embed(title=f"🔍 検索結果: {keyword}", description="\n".join(lines), color=0x2b2d31)
    await ctx.send(embed=embed)

@bot.command()
async def list(ctx):
    """登録済み場所一覧"""
    data = await load_data(ctx.channel.id)
    places = data.get("places", [])
    if not places:
        return await ctx.send("📍 登録されている場所はありません。")
    lines = [f"{i+1}. {p}" for i, p in enumerate(places)]
    embed = discord.Embed(title="📍 登録済み場所", description="\n".join(lines), color=0x2b2d31)
    await ctx.send(embed=embed)

bot.run(TOKEN)
