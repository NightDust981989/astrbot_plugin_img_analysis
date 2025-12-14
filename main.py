from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Image as MsgImage, Reply, Plain
import astrbot.api.message_components as Comp
import aiohttp
import asyncio
from PIL import Image as PILImage
from PIL.ExifTags import TAGS, GPSTAGS
import os
import tempfile
import urllib.parse
from typing import Optional, Tuple


@register(
    "astrbot_plugin_image_metadata",
    "NightDust981989",
    "一个用于解析图片元数据的插件（QQ平台专用）",
    "2.1.0",
    "https://github.com/xxx/astrbot_plugin_image_metadata"
)
class ImageMetadataPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.client: Optional[aiohttp.ClientSession] = None
        self.waiting_sessions = {}  # {user_id: {"timestamp": float, "event": event}}
        self.timeout_tasks = {}     # 存储超时任务
        
        # 加载配置
        if config:
            self.metadata_settings = config.get("metadata_settings", {})
        else:
            self.metadata_settings = getattr(self.context, '_config', {}).get("metadata_settings", {})
        
        # 配置参数
        self.tianditu_api_key = self.metadata_settings.get("tianditu_api_key", "")
        self.timeout_seconds = self.metadata_settings.get("timeout_seconds", 30)
        self.prompt_send_image = self.metadata_settings.get("prompt_send_image", "📷 请发送要解析的图片（30秒内有效）")
        self.prompt_timeout = self.metadata_settings.get("prompt_timeout", "⏰ 解析请求已超时，请重新发送命令")
        self.max_exif_show = self.metadata_settings.get("max_exif_show", 20)
        self.tianditu_api_url = "https://api.tianditu.gov.cn/geocoder"

    async def initialize(self):
        """初始化HTTP客户端"""
        self.client = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )
        logger.info("图片元数据解析插件已初始化（仅支持QQ平台）")

    def _decode_value(self, value) -> str:
        """简单的数值解码，处理中文乱码"""
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8", errors="ignore")
            except:
                return value.decode("gbk", errors="ignore")
        return str(value) if value is not None else "无"

    def _dms_to_dd(self, dms: list, ref: str) -> float:
        """将度分秒(DMS)转换为十进制经纬度(DD)"""
        try:
            deg = float(dms[0])
            minute = float(dms[1])
            sec = float(dms[2]) if len(dms) >= 3 else 0.0
            
            dd = deg + (minute / 60.0) + (sec / 3600.0)
            if ref in ['S', 'W']:
                dd = -dd
            return round(dd, 6)
        except Exception as e:
            logger.warning(f"度分秒转换失败: {e}")
            return 0.0

    def _parse_gps(self, exif_data) -> Tuple[Optional[float], Optional[float], str]:
        """解析Exif中的GPS信息"""
        gps_info = {}
        for tag_id, value in exif_data.items():
            tag_name = GPSTAGS.get(tag_id, str(tag_id))
            gps_info[tag_name] = value

        # 核心GPS字段
        lat_dms = gps_info.get('GPSLatitude')
        lat_ref = gps_info.get('GPSLatitudeRef')
        lon_dms = gps_info.get('GPSLongitude')
        lon_ref = gps_info.get('GPSLongitudeRef')

        if not all([lat_dms, lat_ref, lon_dms, lon_ref]):
            return None, None, "无GPS信息"

        # 转换为十进制经纬度
        latitude = self._dms_to_dd(lat_dms, lat_ref)
        longitude = self._dms_to_dd(lon_dms, lon_ref)

        if latitude == 0.0 and longitude == 0.0:
            return None, None, "GPS坐标无效"

        gps_str = f"纬度：{latitude}° {lat_ref}，经度：{longitude}° {lon_ref}"
        return latitude, longitude, gps_str

    async def _gps_to_address(self, lat: float, lon: float) -> str:
        """调用天地图API解析地址"""
        if not self.tianditu_api_key:
            return "未配置天地图API Key，无法解析地址（请在配置文件中设置tianditu_api_key）"

        try:
            params = {
                "postStr": urllib.parse.quote(f'{{"lon":{lon},"lat":{lat},"ver":1}}'),
                "type": "geocode",
                "tk": self.tianditu_api_key
            }
            async with self.client.get(self.tianditu_api_url, params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()

            if data.get("code") == 0 and data.get("result"):
                result = data["result"]
                province = result.get("province", "")
                city = result.get("city", "")
                district = result.get("district", "")
                street = result.get("street", "")
                number = result.get("number", "")
                
                address_parts = [province, city, district, street, number]
                address_str = "实际地址：" + "".join([p for p in address_parts if p])
                if not address_str.endswith("："):
                    return address_str
                else:
                    return "实际地址：未匹配到详细地址"
            else:
                return f"地址解析失败：{data.get('msg', '未知错误')}（错误码：{data.get('code', '未知')}）"
        except Exception as e:
            logger.error(f"GPS转地址失败: {e}")
            return f"地址解析异常：{str(e)[:50]}..."

    def _parse_image_meta(self, image_path: str) -> dict:
        """解析基础信息、Exif、GPS"""
        result = {
            "basic": {},
            "exif": {},
            "gps": {"lat": None, "lon": None, "str": "无GPS信息"},
            "error": None
        }

        try:
            # 基础文件信息
            result["basic"]["文件大小(KB)"] = round(os.path.getsize(image_path) / 1024, 2)
            
            # PIL解析图片信息
            with PILImage.open(image_path) as img:
                result["basic"]["格式"] = img.format or "未知"
                result["basic"]["分辨率"] = f"{img.width} × {img.height}"
                result["basic"]["色彩模式"] = img.mode or "未知"

                # 解析Exif数据
                exif_data = img.getexif()
                if exif_data:
                    exif_dict = {}
                    gps_exif = {}
                    for tag_id, value in exif_data.items():
                        tag_name = TAGS.get(tag_id, str(tag_id))
                        if tag_name == "GPSInfo":
                            gps_exif = value
                        else:
                            exif_dict[tag_name] = self._decode_value(value)
                    
                    # 解析GPS
                    lat, lon, gps_str = self._parse_gps(gps_exif)
                    result["gps"]["lat"] = lat
                    result["gps"]["lon"] = lon
                    result["gps"]["str"] = gps_str
                    result["exif"] = exif_dict

        except FileNotFoundError:
            result["error"] = "文件未找到"
            logger.error("解析元数据失败: 文件未找到")
        except PILImage.UnidentifiedImageError:
            result["error"] = "无法识别的图片格式"
            logger.error("解析元数据失败: 无法识别的图片格式")
        except Exception as e:
            result["error"] = str(e)[:80]
            logger.error(f"解析元数据失败: {e}")

        return result

    async def _download_image(self, image_url: str) -> Optional[str]:
        """通用图片下载逻辑（无域名限制）"""
        try:
            logger.debug(f"下载图片: {image_url[:100]}...")
            async with self.client.get(image_url) as response:
                if response.status != 200:
                    raise Exception(f"图片下载失败: HTTP {response.status}")
                img_data = await response.read()

            # 保存到临时文件
            temp_file = tempfile.NamedTemporaryFile(suffix=".tmp", delete=False, encoding=None)
            temp_file.write(img_data)
            temp_file.close()
            return temp_file.name
        except asyncio.TimeoutError:
            logger.error("图片下载超时")
            return None
        except Exception as e:
            logger.error(f"下载图片失败: {e}")
            return None

    async def extract_image_from_event(self, event: AstrMessageEvent) -> str:
        """提取QQ平台图片URL（仅处理MsgImage组件，不限制URL域名）"""
        messages = event.get_messages()

        # 1. 处理当前消息中的QQ图片组件
        for msg in messages:
            if isinstance(msg, MsgImage):
                if hasattr(msg, "url") and msg.url:
                    return msg.url.strip()

        # 2. 处理QQ引用消息中的图片
        try:
            for msg in messages:
                if isinstance(msg, Reply):
                    if hasattr(msg, "chain") and msg.chain:
                        for reply_msg in msg.chain:
                            if isinstance(reply_msg, MsgImage) and hasattr(reply_msg, "url") and reply_msg.url:
                                return reply_msg.url.strip()
        except Exception as e:
            logger.warning(f"检查QQ引用消息图片时出错: {e}")

        return None

    async def process_metadata_analysis(self, event: AstrMessageEvent, image_path: str):
        """处理元数据解析并发送结果"""
        try:
            # 解析元数据
            meta = self._parse_image_meta(image_path)

            # 构建消息链
            chain = []
            
            # 基础信息
            basic_lines = ["【基础信息】"]
            for k, v in meta["basic"].items():
                basic_lines.append(f"{k}：{v}")
            chain.append(Comp.Plain("\n".join(basic_lines)))
            chain.append(Comp.Plain("\n"))

            # GPS信息
            gps_lines = ["【GPS信息】", meta["gps"]["str"]]
            if meta["gps"]["lat"] and meta["gps"]["lon"]:
                address_str = await self._gps_to_address(meta["gps"]["lat"], meta["gps"]["lon"])
                gps_lines.append(address_str)
            chain.append(Comp.Plain("\n".join(gps_lines)))
            chain.append(Comp.Plain("\n"))

            # Exif信息
            exif_lines = ["【Exif数据】"]
            if meta["exif"]:
                exif_items = list(meta["exif"].items())[:self.max_exif_show]
                for k, v in exif_items:
                    if v != "无":
                        exif_lines.append(f"{k}：{v}")
                if len(meta["exif"]) > self.max_exif_show:
                    exif_lines.append(f"（共{len(meta['exif'])}个字段，仅展示前{self.max_exif_show}个）")
            else:
                exif_lines.append("无Exif数据")
            chain.append(Comp.Plain("\n".join(exif_lines)))

            # 错误信息
            if meta["error"]:
                chain.append(Comp.Plain(f"\n【解析提示】{meta['error']}"))

            # 发送结果
            await event.send(event.chain_result(chain))

        except Exception as e:
            logger.error(f"处理解析结果失败: {e}")
            await event.send(event.plain_result(f"❌ 解析结果处理失败: {str(e)[:50]}..."))

    @filter.command("imgmeta", "图片元数据", "解析图片元数据")
    async def imgmeta_handler(self, event: AstrMessageEvent, args=None):
        """主指令处理器"""
        user_id = event.get_sender_id()

        # 检查当前消息是否包含QQ图片
        image_url = await self.extract_image_from_event(event)
        if image_url:
            temp_file = await self._download_image(image_url)
            if temp_file:
                await self.process_metadata_analysis(event, temp_file)
                # 清理临时文件
                try:
                    os.unlink(temp_file)
                except:
                    pass
            else:
                await event.send(event.plain_result("❌ 图片下载失败，请重试"))
            return

        # 检查QQ引用消息但无图片的情况
        try:
            raw_event = event._event if hasattr(event, "_event") else event
            if hasattr(raw_event, "reply_to_message") and raw_event.reply_to_message:
                logger.debug("检测到QQ引用消息，但无图片")
                await event.send(event.plain_result("❌ 引用消息中没有找到图片，请确保引用的消息包含图片"))
                return
        except Exception as e:
            logger.warning(f"检查QQ引用消息状态时出错: {e}")

        # 设置等待状态
        self.waiting_sessions[user_id] = {
            "timestamp": asyncio.get_event_loop().time(),
            "event": event,
        }

        # 创建超时任务
        if user_id in self.timeout_tasks:
            self.timeout_tasks[user_id].cancel()

        timeout_task = asyncio.create_task(self.timeout_check(user_id))
        self.timeout_tasks[user_id] = timeout_task

        await event.send(event.plain_result(self.prompt_send_image))
        logger.debug(f"QQ用户 {user_id} 进入等待图片状态，等待{self.timeout_seconds}秒")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听QQ消息，处理等待中的图片解析请求"""
        user_id = event.get_sender_id()

        if user_id not in self.waiting_sessions:
            return

        session = self.waiting_sessions[user_id]

        # 检查超时
        current_time = asyncio.get_event_loop().time()
        if current_time - session["timestamp"] > self.timeout_seconds:
            return

        # 提取QQ图片
        image_url = await self.extract_image_from_event(event)
        if not image_url:
            return

        # 开始解析
        del self.waiting_sessions[user_id]
        if user_id in self.timeout_tasks:
            self.timeout_tasks[user_id].cancel()
            del self.timeout_tasks[user_id]

        temp_file = await self._download_image(image_url)
        if temp_file:
            await self.process_metadata_analysis(event, temp_file)
            try:
                os.unlink(temp_file)
            except:
                pass
        else:
            await event.send(event.plain_result("❌ 图片下载失败，请重试"))

    async def timeout_check(self, user_id: str):
        """超时检查"""
        try:
            await asyncio.sleep(self.timeout_seconds)
            if user_id in self.waiting_sessions:
                session = self.waiting_sessions[user_id]
                event = session["event"]
                del self.waiting_sessions[user_id]
                del self.timeout_tasks[user_id]
                try:
                    await event.send(event.plain_result(self.prompt_timeout))
                    logger.debug(f"QQ用户 {user_id} 的图片解析请求已超时")
                except Exception as send_error:
                    logger.warning(f"发送超时消息失败: {send_error}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"超时检查任务异常: {e}")

    async def terminate(self):
        """插件销毁"""
        if self.client and not self.client.closed:
            await self.client.close()
        for task in self.timeout_tasks.values():
            task.cancel()
        self.timeout_tasks.clear()
        self.waiting_sessions.clear()
        logger.info("图片元数据解析插件已优雅销毁（QQ平台）")