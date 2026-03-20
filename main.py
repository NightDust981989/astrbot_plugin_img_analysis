from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Image as MsgImage, Reply, Plain
import astrbot.api.message_components as Comp
import aiohttp
import asyncio
import os
import tempfile
import json
import subprocess
import sys
from typing import Optional


# 初始化时运行安装检查
def check_and_install_exiftool():
    """检查并安装exiftool"""
    # 检查install.txt文件
    script_dir = os.path.dirname(os.path.abspath(__file__))
    install_txt_path = os.path.join(script_dir, "install.txt")
    install_txt_exists = os.path.exists(install_txt_path)
    
    if install_txt_exists:
        try:
            with open(install_txt_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content == "done":  
                return True
            else:
                logger.warning("exiftool不可用，将尝试自动安装")
        except Exception as e:
            logger.warning(f"读取install.txt时出错: {str(e)}")
    
    # 没有install.txt文件，运行安装程序
    try:
        install_script = os.path.join(os.path.dirname(__file__), "install.py")
        if os.path.exists(install_script):
            logger.info("正在运行install.py进行自动安装...")
            result = subprocess.run([sys.executable, install_script], capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                logger.info("install.py执行完成")
                return True
            else:
                logger.error(f"install.py执行失败: {result.stderr}")
                return False
    except subprocess.TimeoutExpired:
        logger.error("install.py执行超时")
        return False
    except Exception as e:
        logger.error(f"运行install.py时出错: {str(e)}")
        return False


@register(
    "astrbot_plugin_img_analysis",
    "NightDust981989",
    "图片元数据解析插件",
    "1.0.0",
    "https://github.com/NightDust981989/astrbot_plugin_img_analysis"
)
class ImageMetadataPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.client: Optional[aiohttp.ClientSession] = None
        self.waiting_sessions = {}
        self.timeout_tasks = {}
        self.metadata_settings = {}
        
        try:
            if config and isinstance(config, dict):
                self.metadata_settings = config.get("metadata_settings", {})
            else:
                self.metadata_settings = {
                    "amap_api_key": "",
                    "timeout_seconds": 30,
                    "prompt_send_image": "请发送要解析的图片",
                    "prompt_timeout": "解析请求已超时，请重新发送命令",
                    "max_exif_show": 20
                }
        except Exception as e:
            logger.error(f"加载配置失败，使用默认值: {str(e)}")
            self.metadata_settings = {
                "amap_api_key": "",
                "timeout_seconds": 30,
                "prompt_send_image": "请发送要解析的图片",
                "prompt_timeout": "解析请求已超时，请重新发送命令",
                "max_exif_show": 20
            }
        
        self.amap_api_key = self.metadata_settings.get("amap_api_key", "")
        self.timeout_seconds = int(self.metadata_settings.get("timeout_seconds", 30))
        self.prompt_send_image = self.metadata_settings.get("prompt_send_image", "请发送要解析的图片")
        self.prompt_timeout = self.metadata_settings.get("prompt_timeout", "解析请求已超时，请重新发送命令")
        self.max_exif_show = int(self.metadata_settings.get("max_exif_show", 20))
        self.amap_api_url = "https://restapi.amap.com/v3/geocode/regeo"

    async def initialize(self):
        """初始化HTTP客户端"""
        try:
            self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
            logger.info("图片元数据解析插件初始化成功")
            
            # 在初始化时检查并安装exiftool
            loop = asyncio.get_event_loop()
            exiftool_available = await loop.run_in_executor(None, check_and_install_exiftool)
            if not exiftool_available:
                logger.error("exiftool不可用")
                
        except Exception as e:
            logger.error(f"初始化HTTP客户端失败: {str(e)}")
            self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

    def _extract_exiftool_data(self, image_path: str) -> dict:
        """使用exiftool提取元数据"""
        try:
            # 获取所有元数据并输出为JSON格式
            result = subprocess.run([
                'exiftool', 
                '-j',  # JSON输出
                '-a',  # 显示重复的标签
                '-u',  # 显示未知标签
                '-g',  # 按组分组
                image_path
            ], capture_output=True, text=True, timeout=30)
            
            if result.returncode != 0:
                logger.error(f"exiftool执行失败: {result.stderr}")
                return {"error": f"exiftool执行失败: {result.stderr}"}
            
            import json
            data = json.loads(result.stdout)[0]  # 返回数组，取第一个元素

            # 过滤并整理数据
            filtered_data = {}
            for key, value in data.items():
                # 跳过一些内部标签
                if key not in ['SourceFile', 'ExifTool:ExifToolVersion']:
                    filtered_data[key] = str(value)
            
            return filtered_data
        except subprocess.TimeoutExpired:
            logger.error("exiftool执行超时")
            return {"error": "exiftool执行超时"}
        except json.JSONDecodeError:
            logger.error("exiftool输出JSON解析失败")
            return {"error": "JSON解析失败"}
        except Exception as e:
            logger.error(f"exiftool执行出错: {str(e)}")
            return {"error": f"exiftool执行出错: {str(e)}"}

    async def _parse_image_meta(self, image_path: str) -> dict:
        """解析图片元数据"""
        result = {
            "basic": {},
            "camera": {}, 
            "xmp": {},
            "exif": {},
            "gps": {"lat": None, "lon": None, "str": "无GPS信息"},
            "error": None
        }
        try:
            # 获取完整元数据
            exiftool_data = await asyncio.get_event_loop().run_in_executor(None, self._extract_exiftool_data, image_path)
            
            if exiftool_data and not exiftool_data.get("error"):
                # 从数据中提取各类信息
                for key, value in exiftool_data.items():
                    # 基础信息
                    if ':' in key:
                        group, field = key.split(':', 1)
                        
                        # 文件基本信息
                        if group == 'File':
                            if field == 'FileName':
                                result["basic"]["文件名"] = value
                            elif field == 'FileSize':
                                result["basic"]["文件大小"] = value
                            elif field == 'FileType':
                                result["basic"]["文件格式"] = value
                            elif field == 'ImageWidth':
                                result["basic"]["宽度"] = f"{value} 像素"
                            elif field == 'ImageHeight':
                                result["basic"]["高度"] = f"{value} 像素"
                            elif field == 'MIMEType':
                                result["basic"]["MIME类型"] = value
                            elif field == 'ModifyDate':
                                result["basic"]["修改时间"] = value
                        
                        # EXIF信息
                        elif group == 'EXIF':
                            # 基本信息
                            if field == 'Make':
                                result["basic"]["设备厂商"] = value
                            elif field == 'Model':
                                result["basic"]["设备型号"] = value
                            elif field == 'DateTimeOriginal':
                                result["basic"]["拍摄时间"] = value
                            
                            # 相机参数
                            elif field == 'FNumber':
                                result["camera"]["光圈值"] = f"f/{value}"
                            elif field == "ExposureTime":
                                try:
                                    t = float(value)
                                    if t < 1:
                                        result["camera"]["快门"] = f"1/{round(1/t)}s"
                                    else:
                                        result["camera"]["快门"] = f"{t}s"
                                except:
                                    result["camera"]["快门"] = value
                            elif field == 'ISO' or field == 'PhotographicSensitivity':
                                result["camera"]["ISO感光度"] = value
                            elif field == 'FocalLength':
                                result["camera"]["焦距"] = f"{value}mm"
                            elif field == 'Flash':
                                result["camera"]["闪光灯"] = "有" if str(value) != "0" and 'No' not in str(value) else "无"
                            elif field == 'WhiteBalance':
                                result["camera"]["白平衡"] = "手动" if str(value) == "Manual" or str(value) == "1" else "自动"
                            elif field == 'MeteringMode':
                                metering_modes = {
                                    "0": "未知", "1": "平均测光", "2": "中央重点平均测光", 
                                    "3": "点测光", "4": "多点测光", "5": "图案测光", 
                                    "6": "局部测光", "255": "其他", 
                                    "Average": "平均测光", "Center-weighted average": "中央重点平均测光",
                                    "Spot": "点测光", "Multi-segment": "多点测光", "Other": "其他"
                                }
                                result["camera"]["测光模式"] = metering_modes.get(str(value), str(value))
                            elif field == 'ExposureProgram':
                                exposure_programs = {
                                    "0": "未定义", "1": "手动", "2": "程序", "3": "光圈优先", 
                                    "4": "快门优先", "5": "创意程序", "6": "动作程序", 
                                    "7": "人像模式", "8": "风景模式"
                                }
                                result["camera"]["曝光程序"] = exposure_programs.get(str(value), str(value))
                            
                            # 纬度
                            if field == "GPSLatitude":
                                try:
                                    if '°' in value:
                                        lat = self._parse_dms(value)
                                    else:
                                        lat = float(value)
                                    ref = exiftool_data.get("EXIF:GPSLatitudeRef", "N")
                                    if ref in ("S", "s"):
                                        lat = -lat
                                    result["gps"]["lat"] = round(lat, 6)
                                except:    
                                    pass
                            # 经度
                            if field == "GPSLongitude":
                                try:
                                    if '°' in value:
                                        lon = self._parse_dms(value)
                                    else:
                                        lon = float(value)
                                    ref = exiftool_data.get("EXIF:GPSLongitudeRef", "E")
                                    if ref in ("W", "w"):
                                        lon = -lon
                                    result["gps"]["lon"] = round(lon, 6)
                                except:    
                                    pass
                            
                        # XMP信息
                        elif 'xmp' in group.lower() or 'xmp.' in key.lower() or group in ['XMP-xmp', 'XMP-dc', 'XMP-xmpRights', 'XMP-photoshop', 'XMP-crs', 'XMP-aux']:
                            xmp_field = field
                            if 'xmp.xmp' in key.lower():
                                xmp_field = key.split('.')[-1]
                            elif 'xmp-' in key.lower():
                                xmp_field = key.split('-', 1)[-1].split(':', 1)[-1]
                            
                            # 标准化XMP字段名称
                            xmp_display_name = {
                                'Title': '标题',
                                'Description': '描述', 
                                'Subject': '关键词',
                                'Creator': '创作者',
                                'Rights': '版权',
                                'CreateDate': '创建日期',
                                'ModifyDate': '修改日期',
                                'CreatorTool': '创建工具',
                                'Marked': '版权标记',
                                'UsageTerms': '使用条款',
                                'Lens': '镜头型号',
                                'SerialNumber': '序列号',
                                'City': '城市',
                                'State': '州/省',
                                'Country': '国家'
                            }.get(xmp_field, xmp_field)
                            
                            result["xmp"][xmp_display_name] = value
                            
                        # IPTC信息
                        elif group == 'IPTC':
                            iptc_display_name = {
                                'Keywords': '关键词',
                                'By-line': '作者',
                                'Headline': '标题',
                                'Caption-Abstract': '描述',
                                'CopyrightNotice': '版权',
                                'ObjectName': '标题',
                                'City': '城市',
                                'Province-State': '州/省',
                                'Country-PrimaryLocationName': '国家'
                            }.get(field, field)
                            
                            result["xmp"][iptc_display_name] = value
                
                # 更新GPS字符串
                if result["gps"]["lat"] is not None and result["gps"]["lon"] is not None:
                    lat_ref = "N" if result["gps"]["lat"] >= 0 else "S"
                    lon_ref = "E" if result["gps"]["lon"] >= 0 else "W"
                    result["gps"]["str"] = f"纬度：{abs(result['gps']['lat']):.6f}° {lat_ref}，经度：{abs(result['gps']['lon']):.6f}° {lon_ref}"
                else:
                    # 尝试从其他GPS字段获取
                    gps_lat = exiftool_data.get('Composite:GPSLatitude')
                    gps_lon = exiftool_data.get('Composite:GPSLongitude')
                    if gps_lat and gps_lon:
                        try:
                            lat = float(gps_lat)
                            lon = float(gps_lon)
                            lat_ref = "N" if lat >= 0 else "S"
                            lon_ref = "E" if lon >= 0 else "W"
                            result["gps"]["lat"] = lat
                            result["gps"]["lon"] = lon
                            result["gps"]["str"] = f"纬度：{abs(lat):.6f}° {lat_ref}，经度：{abs(lon):.6f}° {lon_ref}"
                        except:
                            pass
                
                # 提取其他有用的信息
                if 'EXIF:Artist' in exiftool_data:
                    result["exif"]["作者"] = exiftool_data['EXIF:Artist']
                if 'EXIF:Copyright' in exiftool_data:
                    result["exif"]["版权"] = exiftool_data['EXIF:Copyright']
                if 'File:Software' in exiftool_data:
                    result["exif"]["编辑软件"] = exiftool_data['File:Software']
                if 'EXIF:LensModel' in exiftool_data:
                    result["exif"]["镜头型号"] = exiftool_data['EXIF:LensModel']
                if 'EXIF:ImageDescription' in exiftool_data:
                    result["exif"]["图片描述"] = exiftool_data['EXIF:ImageDescription']
                
                # 其他EXIF数据
                for key, value in exiftool_data.items():
                    if ':' in key:
                        group, field = key.split(':', 1)
                        if group in ['EXIF', 'MakerNotes', 'QuickTime'] and field not in [
                            'FNumber', 'ExposureTime', 'ISO', 'PhotographicSensitivity', 'FocalLength', 'Flash', 
                            'WhiteBalance', 'MeteringMode', 'ExposureProgram',
                            'GPSLatitude', 'GPSLongitude', 'GPSLatitudeRef', 'GPSLongitudeRef', 
                            'Artist', 'Copyright', 'DateTimeOriginal', 'Make', 'Model',
                            'ImageDescription', 'LensModel'
                        ]:
                            # 格式化显示名称
                            display_field = field.replace('DigitalZoomRatio', '数码变焦').replace('FocalLengthIn35mmFormat', '35mm等效焦距')
                            result["exif"][display_field] = str(value)
            else:
                logger.error(f"exiftool解析失败: {exiftool_data.get('error', '未知错误')}")
                result["error"] = exiftool_data.get("error", "exiftool不可用")

        except Exception as e:
            result["error"] = str(e)[:80]
            logger.error(f"解析元数据失败: {str(e)}")
        return result

    async def _download_image(self, image_url: str) -> Optional[str]:
        """下载图片到临时文件"""
        temp_path = None
        try:
            async with self.client.get(image_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    raise Exception(f"HTTP状态码错误: {resp.status}")
                img_data = await resp.read()

            temp_file = tempfile.NamedTemporaryFile(suffix=".tmp", delete=False)
            temp_file.write(img_data)
            temp_file.close()
            temp_path = temp_file.name
        except Exception as e:
            logger.error(f"下载图片失败: {str(e)}")
        return temp_path
    async def extract_image_from_event(self, event: AstrMessageEvent) -> Optional[str]:
        """提取消息中的图片URL"""
        img_url = None
        try:
            for msg in event.get_messages():
                if isinstance(msg, MsgImage):
                    if hasattr(msg, "url") and msg.url:
                        return msg.url.strip()
                    if hasattr(msg, "file") and msg.file:
                        return msg.file.strip()
                    break
            if not img_url:
                for msg in event.get_messages():
                    if isinstance(msg, Reply) and hasattr(msg, "chain"):
                        for reply_msg in msg.chain:
                            if isinstance(reply_msg, MsgImage) and hasattr(reply_msg, "url") and reply_msg.url:
                                img_url = reply_msg.url.strip()
                                break
        except Exception as e:
            logger.warning(f"提取图片URL失败: {str(e)}")
        return img_url
    
    def _parse_dms(self, dms_str: str) -> float:
        dms_str = dms_str.replace('°', ' ').replace("'", ' ').replace('"', ' ').strip()
        parts = list(map(float, dms_str.split()))
        if len(parts) >= 3:
            return parts[0] + parts[1]/60 + parts[2]/3600
        return float(parts[0]) if parts else 0.0

    async def _process_metadata_analysis(self, event: AstrMessageEvent, image_path: str):
        """处理解析结果并发送"""
        try:
            meta = await self._parse_image_meta(image_path)
            chain = []

            # 基础信息
            if meta["basic"]:
                basic_lines = ["【基础信息】"]
                for k, v in meta["basic"].items():
                    basic_lines.append(f"{k}：{v}")
                chain.append(Comp.Plain("\n".join(basic_lines)))
                chain.append(Comp.Plain("‎\n‎"))

            # 相机参数信息
            if meta["camera"]:  # 只有存在相机参数时才显示
                camera_lines = ["【相机参数】"]
                for k, v in meta["camera"].items():
                    camera_lines.append(f"{k}：{v}")
                chain.append(Comp.Plain("\n".join(camera_lines)))
                chain.append(Comp.Plain("‎\n‎"))

            # GPS信息
            if meta["gps"]["str"] and "无GPS信息" not in meta["gps"]["str"]:
                gps_lines = ["【GPS信息】", meta["gps"]["str"]]
                if meta["gps"]["lat"] and meta["gps"]["lon"]:
                    addr_str = await self._gps_to_address(meta["gps"]["lat"], meta["gps"]["lon"])
                    gps_lines.append(addr_str)
                chain.append(Comp.Plain("\n".join(gps_lines)))
                chain.append(Comp.Plain("‎\n‎"))

            # XMP信息
            if meta["xmp"]:  # 只有存在XMP数据时才显示
                xmp_lines = ["【XMP/IPTC元数据】"]
                for k, v in meta["xmp"].items():
                    xmp_lines.append(f"{k}：{v}")
                chain.append(Comp.Plain("\n".join(xmp_lines)))
                chain.append(Comp.Plain("‎\n‎"))

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
                exif_lines.append("无详细EXIF数据")
            chain.append(Comp.Plain("\n".join(exif_lines)))

            # 错误信息
            if meta["error"]:
                chain.append(Comp.Plain(f"\n【解析提示】\n{meta['error']}"))

            await event.send(event.chain_result(chain))
        except Exception as e:
            logger.error(f"处理解析结果失败: {str(e)}")
            await event.send(event.plain_result(f"解析失败: {str(e)[:50]}..."))

    async def _gps_to_address(self, lat: float, lon: float) -> str:
        """高德地图逆地理编码"""
        if not self.amap_api_key:
            return "未配置高德地图API Key\n请前往 https://lbs.amap.com/ 申请Web服务API密钥，并在配置文件中设置 amap_api_key"

        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            return f"GPS坐标无效\n纬度范围需为[-90,90]，经度范围需为[-180,180]，当前：纬度{lat:.6f}，经度{lon:.6f}"

        resp_str = ""
        try:
            params = {
                "location": f"{lon},{lat}",
                "key": self.amap_api_key,
                "extensions": "all",
                "output": "json",
                "radius": 1000
            }

            async with self.client.get(
                self.amap_api_url,
                params=params,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                response_text = await resp.text()
                logger.debug(f"高德API响应: {response_text[:500]}")
                resp.raise_for_status()
                obj = json.loads(response_text)

            if obj.get("status") == "1":
                regeo = obj.get("regeocode", {})
                formatted_addr = regeo.get("formatted_address", "")
                if formatted_addr:
                    resp_str = f"解析地址：{formatted_addr}"
                else:
                    addr_comp = regeo.get("addressComponent", {})
                    province = addr_comp.get("province", "")
                    city = addr_comp.get("city", "")
                    district = addr_comp.get("district", "")
                    street = addr_comp.get("streetNumber", {}).get("street", "")
                    number = addr_comp.get("streetNumber", {}).get("number", "")
                    addr_parts = [p for p in [province, city, district, street, number] if p]
                    resp_str = "解析地址：" + " ".join(addr_parts) if addr_parts else "解析地址：未匹配到详细地址"
            else:
                resp_str = f"地址解析失败\n错误码：{obj.get('infocode', '未知')}\n错误信息：{obj.get('info', '未知')}"
        except asyncio.TimeoutError:
            resp_str = "地址解析超时（高德API响应超过10秒）"
        except Exception as e:
            resp_str = f"地址解析失败（未知错误）\n{str(e)[:30]}..."
        return resp_str

    @filter.command("imgmeta", "图片元数据", "解析")
    async def imgmeta_handler(self, event: AstrMessageEvent, args=None):
        """主指令"""
        # 兼容不同版本的用户ID获取方式
        try:
            user_id = event.get_sender_id()
        except:
            user_id = str(event.user_id) if hasattr(event, 'user_id') else str(id(event))
        
        img_url = None
        try:
            for msg in event.get_messages():
                if isinstance(msg, MsgImage) and hasattr(msg, "url") and msg.url:
                    img_url = msg.url.strip()
                    break
            if not img_url:
                for msg in event.get_messages():
                    if isinstance(msg, Reply) and hasattr(msg, "chain"):
                        for reply_msg in msg.chain:
                            if isinstance(reply_msg, MsgImage) and hasattr(reply_msg, "url") and reply_msg.url:
                                img_url = reply_msg.url.strip()
                                break
        except Exception as e:
            logger.warning(f"提取图片URL失败: {str(e)}")

        if img_url:
            temp_path = await self._download_image(img_url)
            if temp_path:
                await self._process_metadata_analysis(event, temp_path)
                try:
                    os.unlink(temp_path)
                except:
                    pass
            else:
                await event.send(event.plain_result("图片下载失败"))
            return

        # 等待用户发送图片
        self.waiting_sessions[user_id] = {
            "timestamp": asyncio.get_event_loop().time(),
            "event": event
        }
        if user_id in self.timeout_tasks:
            self.timeout_tasks[user_id].cancel()
            del self.timeout_tasks[user_id]
        self.timeout_tasks[user_id] = asyncio.create_task(self._timeout_check(user_id))
        await event.send(event.plain_result(self.prompt_send_image))

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _on_message(self, event: AstrMessageEvent):
        """监听消息"""
        try:
            user_id = event.get_sender_id()
        except:
            user_id = str(event.user_id) if hasattr(event, 'user_id') else str(id(event))
            
        if user_id not in self.waiting_sessions:
            return

        session = self.waiting_sessions.get(user_id)
        if not session:
            return

        # 检查超时
        if asyncio.get_event_loop().time() - session["timestamp"] > self.timeout_seconds:
            return

        img_url = await self.extract_image_from_event(event)
        if not img_url:
            return

        # 清理等待状态
        del self.waiting_sessions[user_id]
        if user_id in self.timeout_tasks:
            self.timeout_tasks[user_id].cancel()
            del self.timeout_tasks[user_id]

        # 解析图片
        temp_path = await self._download_image(img_url)
        if temp_path:
            await self._process_metadata_analysis(event, temp_path)
            try:
                os.unlink(temp_path)
            except:
                pass
        else:
            await event.send(event.plain_result("图片下载失败"))

    async def _timeout_check(self, user_id: str):
        """超时检查"""
        try:
            await asyncio.sleep(self.timeout_seconds)
            if user_id in self.waiting_sessions:
                event = self.waiting_sessions[user_id]["event"]
                del self.waiting_sessions[user_id]
                if user_id in self.timeout_tasks:
                    del self.timeout_tasks[user_id]
                await event.send(event.plain_result(self.prompt_timeout))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"超时检查失败: {str(e)}")

    async def terminate(self):
        """插件销毁"""
        try:
            if self.client and not self.client.closed:
                await self.client.close()
        except:
            pass
        for task in self.timeout_tasks.values():
            try:
                task.cancel()
            except:
                pass
        self.waiting_sessions.clear()
        self.timeout_tasks.clear()
        logger.info("图片元数据解析插件已优雅销毁")