from aiofiles.os import listdir, path as aiopath, makedirs
from aioshutil import move
from asyncio import sleep, gather, wait_for, TimeoutError as AsyncTimeoutError
from html import escape
from os import path as ospath
from random import choice
from requests import utils as rutils
from time import time

from bot import bot_loop, bot_name, task_dict, task_dict_lock, Intervals, aria2, config_dict, non_queued_up, non_queued_dl, queued_up, queued_dl, queue_dict_lock, LOGGER, DATABASE_URL, bot
from bot.helper.common import TaskConfig
from bot.helper.ext_utils.bot_utils import is_premium_user, UserDaily, default_button, sync_to_async, cmd_exec
from bot.helper.ext_utils.db_handler import DbManager
from bot.helper.ext_utils.files_utils import get_path_size, clean_download, clean_target, join_files
from bot.helper.ext_utils.links_utils import is_magnet, is_url, get_link, is_media, is_gdrive_link, get_stream_link, is_gdrive_id
from bot.helper.ext_utils.shortenurl import short_url
from bot.helper.ext_utils.status_utils import action, get_date_time, get_readable_file_size, get_readable_time
from bot.helper.ext_utils.task_manager import start_from_queued, check_running_tasks
from bot.helper.ext_utils.telegraph_helper import TelePost
from bot.helper.mirror_utils.gdrive_utlis.upload import gdUpload
from bot.helper.mirror_utils.rclone_utils.transfer import RcloneTransferHelper
from bot.helper.mirror_utils.status_utils.gdrive_status import GdriveStatus
from bot.helper.mirror_utils.status_utils.gofile_upload_status import GofileUploadStatus
from bot.helper.mirror_utils.status_utils.queue_status import QueueStatus
from bot.helper.mirror_utils.status_utils.rclone_status import RcloneStatus
from bot.helper.mirror_utils.status_utils.telegram_status import TelegramStatus
from bot.helper.mirror_utils.upload_utils.gofile_uploader import GoFileUploader
from bot.helper.mirror_utils.upload_utils.telegram_uploader import TgUploader
from bot.helper.telegram_helper.button_build import ButtonMaker
from bot.helper.telegram_helper.message_utils import limit, sendCustom, sendMedia, sendMessage, auto_delete_message, sendSticker, sendFile, copyMessage, sendingMessage, update_status_message, delete_status
from bot.helper.video_utils.executor import VidEcxecutor

class TaskListener(TaskConfig):
    def __init__(self):
        super().__init__()

    @staticmethod
    async def clean():
        try:
            if st := Intervals['status']:
                for intvl in list(st.values()):
                    intvl.cancel()
            Intervals['status'].clear()
            await gather(sync_to_async(aria2.purge), delete_status())
        except:
            pass

    def removeFromSameDir(self):
        if self.sameDir and self.mid in self.sameDir['tasks']:
            self.sameDir['tasks'].remove(self.mid)
            self.sameDir['total'] -= 1

    async def onDownloadStart(self):
        if self.isSuperChat and config_dict['INCOMPLETE_TASK_NOTIFIER'] and DATABASE_URL:
            await DbManager().add_incomplete_task(self.message.chat.id, self.message.link, self.tag)

    async def onDownloadComplete(self):
        multi_links = False
        if self.sameDir and self.mid in self.sameDir['tasks']:
            while not (self.sameDir['total'] in [1, 0] or self.sameDir['total'] > 1 and len(self.sameDir['tasks']) > 1):
                await sleep(0.5)

        async with task_dict_lock:
            if self.sameDir and self.sameDir['total'] > 1 and self.mid in self.sameDir['tasks']:
                self.sameDir['tasks'].remove(self.mid)
                self.sameDir['total'] -= 1
                folder_name = self.sameDir['name']
                spath = ospath.join(self.dir, folder_name)
                des_path = ospath.join(f'{config_dict["DOWNLOAD_DIR"]}{list(self.sameDir["tasks"])[0]}', folder_name)
                await makedirs(des_path, exist_ok=True)
                for item in await listdir(spath):
                    if item.endswith(('.aria2', '.!qB')):
                        continue
                    item_path = ospath.join(spath, item)
                    if item in await listdir(des_path):
                        await move(item_path, ospath.join(des_path, f'{self.mid}-{item}'))
                    else:
                        await move(item_path, ospath.join(des_path, item))
                multi_links = True
            task = task_dict[self.mid]
            self.name = task.name()
            gid = task.gid()
        LOGGER.info(f"Download completed: {self.name} (MID: {self.mid})")
        if multi_links:
            await self.onUploadError('Downloaded! Waiting for other tasks.')
            return

        up_path = ospath.join(self.dir, self.name)
        if not await aiopath.exists(up_path):
            try:
                files = await listdir(self.dir)
                self.name = files[-1]
                if self.name == 'yt-dlp-thumb':
                    self.name = files[0]
            except Exception as e:
                await self.onUploadError(str(e))
                return

        await self.isOneFile(up_path)
        await self.reName()

        up_path = ospath.join(self.dir, self.name)
        size = await get_path_size(up_path)

        if not config_dict['QUEUE_ALL'] and not config_dict['QUEUE_COMPLETE']:
            async with queue_dict_lock:
                if self.mid in non_queued_dl:
                    non_queued_dl.remove(self.mid)
            await start_from_queued()

        if self.join and await aiopath.isdir(up_path):
            await join_files(up_path)

        if self.extract:
            up_path = await self.proceedExtract(up_path, size, gid)
            if not up_path:
                return
            up_dir, self.name = ospath.split(up_path)
            size = await get_path_size(up_dir)

        if self.sampleVideo:
            up_path = await self.generateSampleVideo(up_path, gid)
            if not up_path:
                return
            up_dir, self.name = ospath.split(up_path)
            size = await get_path_size(up_dir)

        if self.compress:
            if self.vidMode:
                up_path = await VidEcxecutor(self, up_path, gid).execute()
                if not up_path:
                    return
                self.seed = False
            up_path = await self.proceedCompress(up_path, size, gid)
            if not up_path:
                return

        if not self.compress and self.vidMode:
            LOGGER.info(f"Processing video with VidEcxecutor for MID: {self.mid}")
            up_path = await VidEcxecutor(self, up_path, gid).execute()
            if not up_path:
                return
            self.seed = False
            up_dir, self.name = ospath.split(up_path)
            size = await get_path_size(up_dir)

        # Splitting logic for large files
        o_files, m_size = [], []
        TELEGRAM_LIMIT = 2 * 1024 * 1024 * 1024  # 2 GB in bytes (2,147,483,648)
        DEFAULT_SPLIT_SIZE = 2097152000  # 2 GB exact, per LEECH_SPLIT_SIZE
        split_size = config_dict.get('LEECH_SPLIT_SIZE', DEFAULT_SPLIT_SIZE)
        if is_premium_user(self.user_id) and 'PREMIUM_SPLIT_SIZE' in config_dict:
            split_size = config_dict['PREMIUM_SPLIT_SIZE']  # e.g., 4 GB for premium
        split_size = min(split_size, TELEGRAM_LIMIT)  # Cap at Telegram's 2 GB limit

        if size > TELEGRAM_LIMIT and await aiopath.isfile(up_path):
            LOGGER.info(f"Splitting file {self.name} (size: {size}) into parts of {split_size} bytes")
            o_files, m_size = await self._split_file(up_path, up_dir, split_size)
            if not o_files:
                await self.onUploadError(f"Failed to split {self.name} into parts.")
                return
            for f_size in m_size:
                if f_size > TELEGRAM_LIMIT:
                    LOGGER.error(f"Split file size {f_size} exceeds Telegram limit of {TELEGRAM_LIMIT} bytes")
                    await self.onUploadError("Split file exceeds Telegram 2 GB limit.")
                    return
        else:
            o_files.append(self.name)
            m_size.append(size)

        LOGGER.info(f"Leeching {self.name} (MID: {self.mid}) with o_files: {o_files}, m_size: {m_size}")
        tg = TgUploader(self, up_dir, size)
        async with task_dict_lock:
            task_dict[self.mid] = TelegramStatus(self, tg, size, gid, 'up')
        try:
            await wait_for(gather(update_status_message(self.message.chat.id), tg.upload(o_files, m_size)), timeout=600)
            LOGGER.info(f"Leech Completed: {self.name} (MID: {self.mid})")
        except AsyncTimeoutError:
            LOGGER.error(f"Upload timeout for MID: {self.mid}")
            await self.onUploadError("Upload timed out after 10 minutes.")
            return
        except Exception as e:
            LOGGER.error(f"Upload error for MID: {self.mid}: {e}", exc_info=True)
            await self.onUploadError(f"Upload failed: {str(e)}")
            return

        await clean_download(self.dir)
        async with task_dict_lock:
            task_dict.pop(self.mid, None)
        async with queue_dict_lock:
            if self.mid in non_queued_up:
                non_queued_up.remove(self.mid)
        await start_from_queued()

    async def _split_file(self, file_path, up_dir, split_size):
        try:
            TELEGRAM_LIMIT = 2 * 1024 * 1024 * 1024  # 2 GB (2,147,483,648 bytes)
            file_size = await get_path_size(file_path)
            if file_size <= TELEGRAM_LIMIT:
                return [ospath.basename(file_path)], [file_size]

            base_name = ospath.splitext(ospath.basename(file_path))[0]
            split_size = min(split_size, TELEGRAM_LIMIT - 1024 * 1024)  # 1 MB buffer

            # Step 1: Split file into raw chunks using Unix split
            temp_dir = ospath.join(up_dir, "split_temp")
            await makedirs(temp_dir, exist_ok=True)
            chunk_prefix = ospath.join(temp_dir, f"{base_name}_chunk")
            cmd_split = ['split', '-b', str(split_size), file_path, chunk_prefix]
            _, stderr, rcode = await cmd_exec(cmd_split)
            if rcode != 0:
                LOGGER.error(f"Unix split failed for {file_path}: {stderr}")
                await clean_target(temp_dir)
                return [], []

            # Step 2: Fix each chunk with FFmpeg to make valid MKV files
            o_files, m_size = [], []
            chunk_files = [ospath.join(temp_dir, f) for f in await listdir(temp_dir) if f.startswith(f"{base_name}_chunk")]
            for i, chunk in enumerate(chunk_files):
                output_file = ospath.join(up_dir, f"{base_name}_part{i:03d}.mkv")
                cmd_ffmpeg = [
                    'ffmpeg', '-i', chunk, '-c', 'copy', '-map', '0',
                    '-f', 'matroska', output_file, '-y'
                ]
                _, stderr, rcode = await cmd_exec(cmd_ffmpeg)
                if rcode == 0:
                    part_size = await get_path_size(output_file)
                    if part_size <= TELEGRAM_LIMIT:
                        o_files.append(ospath.basename(output_file))
                        m_size.append(part_size)
                    else:
                        LOGGER.warning(f"Part {output_file} exceeds {TELEGRAM_LIMIT} bytes, removing")
                        await clean_target(output_file)
                else:
                    LOGGER.error(f"FFmpeg fix failed for chunk {chunk}: {stderr}")
                    await clean_target(output_file)

            await clean_target(temp_dir)  # Clean up temporary chunks
            if not o_files:
                LOGGER.error(f"No valid split files generated for {file_path}")
                return [], []

            LOGGER.info(f"Split {file_path} into {len(o_files)} parts: {o_files}")
            return o_files, m_size
        except Exception as e:
            LOGGER.error(f"Split file error for {file_path}: {e}", exc_info=True)
            await clean_target(temp_dir) if 'temp_dir' in locals() else None
            return [], []

    async def onUploadComplete(self, link, size, files, folders, mime_type, rclonePath='', dir_id=''):
        if self.isSuperChat and config_dict['INCOMPLETE_TASK_NOTIFIER'] and DATABASE_URL:
            await DbManager().rm_complete_task(self.message.link)

        LOGGER.info(f"Task Done: {self.name} (MID: {self.mid})")
        dt_date, dt_time = get_date_time(self.message)
        buttons = ButtonMaker()
        buttons_scr = ButtonMaker()
        daily_size = size
        size_str = get_readable_file_size(size)
        reply_to = self.message.reply_to_message
        images = choice(config_dict['IMAGE_COMPLETE'].split())
        TIME_ZONE_TITLE = config_dict['TIME_ZONE_TITLE']

        thumb_path = ospath.join(self.dir, 'thumb.png')
        if not await aiopath.exists(thumb_path):
            LOGGER.info(f"Thumbnail not found at {thumb_path}, using default")
            thumb_path = None

        msg = f'<a href="https://t.me/satyamisme1"><b><i>Bot By satyamisme</b></i></a>\n'
        msg += f'<code>{escape(self.name)}</code>\n'
        msg += f'<b>┌ Size: </b>{size_str}\n'

        if self.isLeech:
            if config_dict['SOURCE_LINK']:
                scr_link = get_link(self.message)
                if is_magnet(scr_link):
                    tele = TelePost(config_dict['SOURCE_LINK_TITLE'])
                    mag_link = await sync_to_async(tele.create_post, f'<code>{escape(self.name)}<br>({size_str})</code><br>{scr_link}')
                    buttons.button_link('Source Link', mag_link)
                    buttons_scr.button_link('Source Link', mag_link)
                elif is_url(scr_link):
                    buttons.button_link('Source Link', scr_link)
                    buttons_scr.button_link('Source Link', scr_link)
            if self.user_dict.get('enable_pm') and self.isSuperChat:
                buttons.button_link('View File(s)', f'http://t.me/{bot_name}')
            msg += f'<b>├ Total Files: </b>{folders}\n'
            if mime_type and mime_type != 0:
                msg += f'<b>├ Corrupted Files: </b>{mime_type}\n'
            msg += (f'<b>├ Elapsed: </b>{get_readable_time(time() - self.message.date.timestamp())}\n'
                    f'<b>├ Cc: </b>{self.tag}\n'
                    f'<b>└ Action: </b>{action(self.message)}\n\n')
            if files:
                fmsg = '<b>Leech File(s):</b>\n'
                for index, (tlink, name) in enumerate(files.items(), start=1):
                    fmsg += f'{index}. <a href="{tlink}">{name}</a>\n'
                msg += fmsg
            uploadmsg = await sendingMessage(msg, self.message, images if not thumb_path else thumb_path, buttons.build_menu(2))
        else:
            msg += f'<b>├ Type: </b>{mime_type or "File"}\n'
            if mime_type == 'Folder':
                if folders:
                    msg += f'<b>├ SubFolders: </b>{folders}\n'
                msg += f'<b>├ Files: </b>{files}\n'
            msg += (f'<b>├ Elapsed: </b>{get_readable_time(time() - self.message.date.timestamp())}\n'
                    f'<b>├ Cc: </b>{self.tag}\n'
                    f'<b>└ Action: </b>{action(self.message)}\n')
            if link:
                buttons.button_link('Cloud Link', link)
            elif rclonePath:
                msg += f'\n\n<b>Path:</b> <code>{rclonePath}</code>'
            uploadmsg = await sendingMessage(msg, self.message, images if not thumb_path else thumb_path, buttons.build_menu(2))

        if self.user_dict.get('enable_pm') and self.isSuperChat:
            await copyMessage(self.user_id, uploadmsg, buttons_scr.build_menu(2))
        if chat_id := config_dict.get('LEECH_LOG') if self.isLeech else config_dict.get('MIRROR_LOG'):
            await copyMessage(chat_id, uploadmsg)

        await clean_download(self.dir)
        async with task_dict_lock:
            task_dict.pop(self.mid, None)
            count = len(task_dict)
        if count == 0:
            await self.clean()
        else:
            await update_status_message(self.message.chat.id)

        async with queue_dict_lock:
            if self.mid in non_queued_dl:
                non_queued_dl.remove(self.mid)
            if self.mid in non_queued_up:
                non_queued_up.remove(self.mid)
        await start_from_queued()

        if self.isSuperChat and (stime := config_dict['AUTO_DELETE_UPLOAD_MESSAGE_DURATION']):
            bot_loop.create_task(auto_delete_message(self.message, uploadmsg, reply_to, stime=stime))

    async def onDownloadError(self, error, listfile=None):
        LOGGER.error(f"Download error for MID: {self.mid}: {error}")
        async with task_dict_lock:
            task_dict.pop(self.mid, None)
        await self.clean()
        if self.isSuperChat and DATABASE_URL:
            await DbManager().rm_complete_task(self.message.link)
        await sendingMessage(f"Download failed: {error}", self.message, choice(config_dict['IMAGE_COMPLETE'].split()))
        await gather(start_from_queued(), clean_download(self.dir))

    async def onUploadError(self, error):
        LOGGER.error(f"Upload error for MID: {self.mid}: {error}")
        async with task_dict_lock:
            task_dict.pop(self.mid, None)
        await self.clean()
        if self.isSuperChat and DATABASE_URL:
            await DbManager().rm_complete_task(self.message.link)
        await sendingMessage(f"Upload failed: {error}", self.message, choice(config_dict['IMAGE_COMPLETE'].split()))
        await gather(start_from_queued(), clean_download(self.dir))