from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Image as MsgImage, Reply, Plain
import astrbot.api.message_components as Comp
import aiohttp
import asyncio
import exifread
import os
import tempfile
import urllib.parse
import json
from typing import Optional, Tuple


@register(
    "astrbot_plugin_image_metadata",
    "NightDust981989",
    "一个用于解析图片元数据的插件（QQ平台专用）",
    "4.0.0",
    "https://github.com/xxx/astrbot_plugin_image_metadata"
)
class ImageMetadataPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.client: Optional[aiohttp.ClientSession] = None
        self.waiting_sessions = {}
        self.timeout_tasks = {}
        
        # 加载配置（适配高德API密钥）
        if config:
            self.metadata_settings = config.get("metadata_settings", {})
        else:
            self.metadata_settings = getattr(self.context, '_config', {}).get("metadata_settings", {})
        
        # 配置参数（替换为高德API相关）
        self.amap_api_key = self.metadata_settings.get("amap_api_key", "")  # 高德API密钥
        self.timeout_seconds = self.metadata_settings.get("timeout_seconds", 30)
        self.prompt_send_image = self.metadata_settings.get("prompt_send_image", "📷 请发送要解析的图片（30秒内有效）")
        self.prompt_timeout = self.metadata_settings.get("prompt_timeout", "⏰ 解析请求已超时，请重新发送命令")
        self.max_exif_show = self.metadata_settings.get("max_exif_show", 20)
        # 高德逆地理编码API地址
        self.amap_api_url = "https://restapi.amap.com/v3/geocode/regeo"

    async def initialize(self):
        """初始化HTTP客户端"""
        connector = aiohttp.TCPConnector(ssl=False)
        self.client = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=30)
        )
        logger.info("图片元数据解析插件已初始化（使用exifread解析GPS + 高德地图API）")

    def _convert_exif_gps(self, gps_coords, ref) -> float:
        """将Exif格式的GPS坐标转换为十进制（限制6位小数）"""
        try:
            deg = float(gps_coords.values[0].num) / float(gps_coords.values[0].den)
            min = float(gps_coords.values[1].num) / float(gps_coords.values[1].den)
            sec = float(gps_coords.values[2].num) / float(gps_coords.values[2].den)
            
            dd = deg + (min / 60.0) + (sec / 3600.0)
            if ref in ['S', 'W']:
                dd = -dd
            return round(dd, 6)
        except Exception as e:
            logger.warning(f"GPS坐标转换失败: {e}")
            return 0.0

    def _parse_gps_exifread(self, exif_tags) -> Tuple[Optional[float], Optional[float], str]:
        """使用exifread解析GPS"""
        try:
            gps_lat = exif_tags.get('GPS GPSLatitude')
            gps_lat_ref = exif_tags.get('GPS GPSLatitudeRef')
            gps_lon = exif_tags.get('GPS GPSLongitude')
            gps_lon_ref = exif_tags.get('GPS GPSLongitudeRef')

            if not all([gps_lat, gps_lat_ref, gps_lon, gps_lon_ref]):
                logger.debug("Exif中缺失GPS字段")
                return None, None, "无GPS信息"
            
            latitude = self._convert_exif_gps(gps_lat, gps_lat_ref.values)
            longitude = self._convert_exif_gps(gps_lon, gps_lon_ref.values)

            if latitude == 0.0 and longitude == 0.0:
                return None, None, "GPS坐标无效（值为0）"

            gps_str = f"纬度：{latitude}° {gps_lat_ref.values}，经度：{longitude}° {gps_lon_ref.values}"
            return latitude, longitude, gps_str
        except Exception as e:
            logger.error(f"解析GPS失败: {e}")
            return None, None, f"GPS解析异常: {str(e)[:20]}..."

    async def _gps_to_address(self, lat: float, lon: float) -> str:
        """高德地图逆地理编码API调用（替换天地图）"""
        if not self.amap_api_key:
            return "❌ 未配置高德地图API Key\n请前往 https://lbs.amap.com/ 申请Web服务API密钥，并在配置文件中设置 amap_api_key"

        # 基础参数校验
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            return f"❌ GPS坐标无效\n纬度范围需为[-90,90]，经度范围需为[-180,180]，当前：纬度{lat}，经度{lon}"

        try:
            # 高德API参数构建（经纬度格式：lon,lat）
            params = {
                "location": f"{lon},{lat}",  # 高德要求 经度,纬度 顺序
                "key": self.amap_api_key,
                "extensions": "all",  # 返回详细地址信息
                "output": "json",
                "radius": 1000
            }
            
            # 打印调试信息
            logger.debug(f"高德API请求参数: {params}")
            
            # 发送GET请求
            async with self.client.get(
                self.amap_api_url,
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json"
                }
            ) as resp:
                logger.debug(f"高德API响应状态码: {resp.status}")
                response_text = await resp.text()
                logger.debug(f"高德API原始响应: {response_text[:500]}")
                
                resp.raise_for_status()
                data = json.loads(response_text)

            # 解析高德API响应
            if data.get("status") == "1":  # 高德API成功状态码为"1"
                regeo_data = data.get("regeocode", {})
                # 提取详细地址
                formatted_address = regeo_data.get("formatted_address", "")
                if formatted_address:
                    address_str = f"📍 解析地址：{formatted_address}"
                else:
                    # 分级提取地址
                    address_component = regeo_data.get("addressComponent", {})
                    province = address_component.get("province", "")
                    city = address_component.get("city", "")
                    district = address_component.get("district", "")
                    township = address_component.get("township", "")
                    street = address_component.get("streetNumber", {}).get("street", "")
                    number = address_component.get("streetNumber", {}).get("number", "")
                    
                    address_parts = [p for p in [province, city, district, township, street, number] if p]
                    if address_parts:
                        address_str = f"📍 解析地址：{' '.join(address_parts)}"
                    else:
                        address_str = "📍 解析地址：未匹配到详细地址"
                
                # 补充兴趣点信息（可选）
                pois = regeo_data.get("pois", [])
                if pois and len(pois) > 0:
                    address_str += f"\n📌 附近兴趣点：{pois[0].get('name', '')}（{pois[0].get('type', '')}）"
                
                return address_str
            else:
                error_info = data.get("info", "未知错误")
                error_code = data.get("infocode", "未知码")
                return f"❌ 地址解析失败\n错误码：{error_code}\n错误信息：{error_info}"

        except aiohttp.ClientError as e:
            logger.error(f"高德API网络错误: {str(e)}")
            return f"❌ 地址解析失败（网络错误）\n{str(e)[:30]}...\n请检查网络或稍后重试"
        except asyncio.TimeoutError:
            return "❌ 地址解析超时（高德API响应超过10秒）"
        except json.JSONDecodeError as e:
            logger.error(f"高德API响应JSON解析失败: {str(e)} | 响应: {response_text[:100]}")
            return f"❌ 地址解析失败（响应格式错误）\n{str(e)[:30]}..."
        except Exception as e:
            logger.error(f"高德API调用未知错误: {str(e)}")
            return f"❌ 地址解析失败（未知错误）\n{str(e)[:30]}..."

    def _parse_image_meta(self, image_path: str) -> dict:
        """使用exifread解析完整Exif数据"""
        result = {
            "basic": {},
            "exif": {},
            "gps": {"lat": None, "lon": None, "str": "无GPS信息"},
            "error": None
        }

        try:
            # 基础文件信息
            file_size = os.path.getsize(image_path)
            result["basic"]["文件大小(KB)"] = round(file_size / 1024, 2)
            result["basic"]["文件大小(MB)"] = round(file_size / 1024 / 1024, 2)

            # 解析Exif
            with open(image_path, 'rb') as f:
                exif_tags = exifread.process_file(f, details=False)
            
            # 提取基础图片信息
            if exif_tags.get('Image ImageWidth'):
                result["basic"]["宽度"] = f"{exif_tags['Image ImageWidth'].values} 像素"
            if exif_tags.get('Image ImageLength'):
                result["basic"]["高度"] = f"{exif_tags['Image ImageLength'].values} 像素"
            if exif_tags.get('Image FileType'):
                result["basic"]["格式"] = exif_tags['Image FileType'].values
            if exif_tags.get('Image Make'):
                result["basic"]["设备厂商"] = exif_tags['Image Make'].values
            if exif_tags.get('Image Model'):
                result["basic"]["设备型号"] = exif_tags['Image Model'].values
            if exif_tags.get('Image DateTime'):
                result["basic"]["拍摄时间"] = exif_tags['Image DateTime'].values

            # 解析GPS
            lat, lon, gps_str = self._parse_gps_exifread(exif_tags)
            result["gps"]["lat"] = lat
            result["gps"]["lon"] = lon
            result["gps"]["str"] = gps_str

            # 提取其他Exif字段
            exif_dict = {}
            for tag, value in exif_tags.items():
                if not tag.startswith('GPS') and not isinstance(value.values, bytes):
                    exif_dict[tag.replace(' ', '_')] = str(value.values)
            
            result["exif"] = exif_dict

        except Exception as e:
            result["error"] = str(e)[:80]
            logger.error(f"解析元数据失败: {e}")

        return result

    async def _download_image(self, image_url: str) -> Optional[str]:
        """下载图片到临时文件"""
        try:
            logger.debug(f"下载图片: {image_url[:100]}...")
            async with self.client.get(image_url) as response:
                if response.status != 200:
                    raise Exception(f"图片下载失败: HTTP {response.status}")
                img_data = await response.read()

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
        """提取QQ消息中的图片URL"""
        messages = event.get_messages()

        # 1. 处理当前消息中的图片
        for msg in messages:
            if isinstance(msg, MsgImage):
                if hasattr(msg, "url") and msg.url:
                    return msg.url.strip()

        # 2. 处理引用消息中的图片
        try:
            for msg in messages:
                if isinstance(msg, Reply):
                    if hasattr(msg, "chain") and msg.chain:
                        for reply_msg in msg.chain:
                            if isinstance(reply_msg, MsgImage) and hasattr(reply_msg, "url") and reply_msg.url:
                                return reply_msg.url.strip()
        except Exception as e:
            logger.warning(f"检查引用消息图片时出错: {e}")

        return None

    async def process_metadata_analysis(self, event: AstrMessageEvent, image_path: str):
        """处理元数据解析并发送结果"""
        try:
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
            exif_lines = ["【Exif详细数据】"]
            if meta["exif"]:
                exif_items = list(meta["exif"].items())[:self.max_exif_show]
                for k, v in exif_items:
                    if v and v != "None":
                        exif_lines.append(f"{k}：{v}")
                if len(meta["exif"]) > self.max_exif_show:
                    exif_lines.append(f"（共{len(meta['exif'])}个字段，仅展示前{self.max_exif_show}个）")
            else:
                exif_lines.append("无Exif详细数据")
            chain.append(Comp.Plain("\n".join(exif_lines)))

            # 错误信息
            if meta["error"]:
                chain.append(Comp.Plain(f"\n【解析提示】{meta['error']}"))

            await event.send(event.chain_result(chain))

        except Exception as e:
            logger.error(f"处理解析结果失败: {e}")
            await event.send(event.plain_result(f"❌ 解析结果处理失败: {str(e)[:50]}..."))

    @filter.command("imgmeta", "图片元数据", "解析图片元数据")
    async def imgmeta_handler(self, event: AstrMessageEvent, args=None):
        """主指令处理器"""
        user_id = event.get_sender_id()

        # 检查当前消息是否包含图片
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

        # 检查引用消息无图片的情况
        try:
            raw_event = event._event if hasattr(event, "_event") else event
            if hasattr(raw_event, "reply_to_message") and raw_event.reply_to_message:
                await event.send(event.plain_result("❌ 引用消息中没有找到图片，请确保引用的消息包含图片"))
                return
        except Exception as e:
            logger.warning(f"检查引用消息状态时出错: {e}")

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
        """监听消息，处理等待中的图片解析请求"""
        user_id = event.get_sender_id()

        if user_id not in self.waiting_sessions:
            return

        session = self.waiting_sessions[user_id]

        # 检查超时
        current_time = asyncio.get_event_loop().time()
        if current_time - session["timestamp"] > self.timeout_seconds:
            return

        # 提取图片
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
        logger.info("图片元数据解析插件已优雅销毁（QQ平台 + 高德地图API）")