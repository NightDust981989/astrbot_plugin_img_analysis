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
import json
from typing import Optional, Tuple
# 添加XMP处理库
try:
    from lxml import etree
except ImportError:
    etree = None


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
            connector = aiohttp.TCPConnector(ssl=False)
            self.client = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=30)
            )
            logger.info("图片元数据解析插件初始化成功")
        except Exception as e:
            logger.error(f"初始化HTTP客户端失败: {str(e)}")
            self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

    def _safe_get_exif_value(self, tag_value) -> str:
        """安全获取Exif值，处理bytes类型和Tag对象"""
        try:
            # 如果是bytes类型，尝试解码为字符串
            if isinstance(tag_value, bytes):
                # 优先尝试UTF-8解码，失败则用GBK，最后返回十六进制
                try:
                    return tag_value.decode('utf-8', errors='ignore').strip()
                except:
                    try:
                        return tag_value.decode('gbk', errors='ignore').strip()
                    except:
                        return f"[二进制数据] 长度: {len(tag_value)} bytes"
            
            # 如果是exifread的Tag对象，读取values属性
            if hasattr(tag_value, 'values'):
                # 处理values是列表的情况
                if isinstance(tag_value.values, list):
                    # 列表元素如果是bytes，解码后拼接
                    values = []
                    for v in tag_value.values:
                        if isinstance(v, bytes):
                            values.append(self._safe_get_exif_value(v))
                        else:
                            values.append(str(v))
                    return ", ".join(values)
                # 普通values值
                return str(tag_value.values).strip()
            
            # 其他类型直接转字符串
            return str(tag_value).strip()
        except Exception as e:
            logger.warning(f"解析Exif值失败: {str(e)}")
            return f"[解析失败] {str(e)[:10]}"

    def _convert_exif_gps(self, gps_coords, ref) -> float:
        """GPS坐标转换为十进制（增加安全取值）"""
        obj = 0.0
        try:
            # 安全获取GPS度分秒值
            deg_val = self._safe_get_exif_value(gps_coords.values[0])
            min_val = self._safe_get_exif_value(gps_coords.values[1])
            sec_val = self._safe_get_exif_value(gps_coords.values[2])
            
            # 转换为浮点数
            deg = float(deg_val) if deg_val.replace('.', '').isdigit() else 0.0
            min_v = float(min_val) if min_val.replace('.', '').isdigit() else 0.0
            sec_v = float(sec_val) if sec_val.replace('.', '').isdigit() else 0.0
            
            obj = deg + (min_v / 60.0) + (sec_v / 3600.0)
            if ref in ['S', 'W']:
                obj = -obj
            obj = round(obj, 6)
        except Exception as e:
            logger.warning(f"GPS坐标转换失败: {str(e)}")
            obj = 0.0
        return obj

    def _parse_gps_exifread(self, exif_tags) -> Tuple[Optional[float], Optional[float], str]:
        """解析GPS信息（处理Tag对象）"""
        lat = None
        lon = None
        gps_str = "无GPS信息"
        try:
            gps_lat = exif_tags.get('GPS GPSLatitude')
            gps_lat_ref = exif_tags.get('GPS GPSLatitudeRef')
            gps_lon = exif_tags.get('GPS GPSLongitude')
            gps_lon_ref = exif_tags.get('GPS GPSLongitudeRef')

            # 检查是否都是有效的Tag对象
            if all([
                gps_lat and hasattr(gps_lat, 'values'),
                gps_lat_ref and hasattr(gps_lat_ref, 'values'),
                gps_lon and hasattr(gps_lon, 'values'),
                gps_lon_ref and hasattr(gps_lon_ref, 'values')
            ]):
                # 安全获取参考值
                lat_ref = self._safe_get_exif_value(gps_lat_ref.values)
                lon_ref = self._safe_get_exif_value(gps_lon_ref.values)
                
                lat = self._convert_exif_gps(gps_lat, lat_ref)
                lon = self._convert_exif_gps(gps_lon, lon_ref)
                
                if lat == 0.0 and lon == 0.0:
                    gps_str = "GPS坐标无效（值为0）"
                else:
                    gps_str = f"纬度：{lat}° {lat_ref}，经度：{lon}° {lon_ref}"
            else:
                gps_str = "无GPS信息"
        except Exception as e:
            logger.error(f"解析GPS失败: {str(e)}")
            gps_str = f"GPS解析异常: {str(e)[:20]}..."
        return lat, lon, gps_str

    def _extract_xmp_data(self, image_path: str) -> dict:
        """提取XMP元数据"""
        xmp_data = {}
        try:
            if not etree:
                return {"error": "缺少lxml库，无法解析XMP数据"}
                
            with open(image_path, 'rb') as f:
                data = f.read()
            
            # 查找XMP数据块
            xmp_start = data.find(b'<x:xmpmeta')
            if xmp_start == -1:
                xmp_start = data.find(b'<rdf:RDF')
            if xmp_start == -1:
                xmp_start = data.find(b'<xmpmeta')
            
            if xmp_start != -1:
                # 找到结束标签
                xmp_end = data.find(b'</x:xmpmeta>', xmp_start)
                if xmp_end == -1:
                    xmp_end = data.find(b'</rdf:RDF>', xmp_start)
                if xmp_end != -1:
                    xmp_end += len(b'</x:xmpmeta>') if b'</x:xmpmeta>' in data[xmp_start:xmp_end+20] else len(b'</rdf:RDF>')
                    xmp_content = data[xmp_start:xmp_end]
                    
                    # 解析XML
                    parser = etree.XMLParser(recover=True)
                    root = etree.fromstring(xmp_content, parser=parser)
                    
                    # 定义常用的XMP命名空间
                    namespaces = {
                        'dc': 'http://purl.org/dc/elements/1.1/',
                        'xmp': 'http://ns.adobe.com/xap/1.0/',
                        'xmpRights': 'http://ns.adobe.com/xap/1.0/rights/',
                        'xmpMM': 'http://ns.adobe.com/xap/1.0/mm/',
                        'photoshop': 'http://ns.adobe.com/photoshop/1.0/',
                        'tiff': 'http://ns.adobe.com/tiff/1.0/',
                        'iptc': 'http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/',
                        'crs': 'http://ns.adobe.com/camera-raw-settings/1.0/',
                        'aux': 'http://ns.adobe.com/exif/1.0/aux/'
                    }
                    
                    # 提取常见的XMP字段
                    # 标题和描述
                    title = root.find('.//dc:title/rdf:Alt/rdf:li', namespaces)
                    if title is not None:
                        xmp_data['标题'] = title.text
                        
                    desc = root.find('.//dc:description/rdf:Alt/rdf:li', namespaces)
                    if desc is not None:
                        xmp_data['描述'] = desc.text
                    
                    # 关键词
                    keywords = root.find('.//dc:subject/rdf:Bag', namespaces)
                    if keywords is not None:
                        keyword_list = []
                        for li in keywords.findall('rdf:li', namespaces):
                            if li.text:
                                keyword_list.append(li.text)
                        if keyword_list:
                            xmp_data['关键词'] = ', '.join(keyword_list)
                    
                    # 作者和创作者
                    creator = root.find('.//dc:creator/rdf:Seq/rdf:li', namespaces)
                    if creator is not None:
                        xmp_data['创作者'] = creator.text
                    
                    # 版权信息
                    rights = root.xpath('//xmpRights:Marked | //xmpRights:UsageTerms/rdf:Alt/rdf:li', namespaces)
                    for right in rights:
                        if right.tag.endswith('Marked'):
                            xmp_data['版权标记'] = '是' if right.text and right.text.lower() in ['true', 'yes'] else '否'
                        elif right.tag.endswith('UsageTerms'):
                            xmp_data['使用条款'] = right.text
                    
                    # 创建时间
                    create_date = root.find('.//xmp:CreateDate', namespaces)
                    if create_date is not None:
                        xmp_data['创建日期'] = create_date.text
                    
                    # 修改时间
                    mod_date = root.find('.//xmp:ModifyDate', namespaces)
                    if mod_date is not None:
                        xmp_data['修改日期'] = mod_date.text
                    
                    # 创建者工具
                    creator_tool = root.find('.//xmp:CreatorTool', namespaces)
                    if creator_tool is not None:
                        xmp_data['创建工具'] = creator_tool.text
                    
                    # 相机原始设置（如果存在）
                    lens_model = root.find('.//aux:Lens', namespaces)
                    if lens_model is not None:
                        xmp_data['镜头型号'] = lens_model.text
                    
                    serial_number = root.find('.//aux:SerialNumber', namespaces)
                    if serial_number is not None:
                        xmp_data['序列号'] = serial_number.text
                    
                    # IPTC信息
                    city = root.find('.//photoshop:City', namespaces)
                    if city is not None:
                        xmp_data['城市'] = city.text
                    
                    state = root.find('.//photoshop:State', namespaces)
                    if state is not None:
                        xmp_data['州/省'] = state.text
                    
                    country = root.find('.//photoshop:Country', namespaces)
                    if country is not None:
                        xmp_data['国家'] = country.text
            
        except Exception as e:
            logger.warning(f"解析XMP数据失败: {str(e)}")
            xmp_data['error'] = f"XMP解析错误: {str(e)}"
        
        return xmp_data

    async def _gps_to_address(self, lat: float, lon: float) -> str:
        """高德地图逆地理编码"""
        if not self.amap_api_key:
            return "未配置高德地图API Key\n请前往https://lbs.amap.com/申请Web服务API密钥，并在配置文件中设置 amap_api_key"

        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            return f"GPS坐标无效\n纬度范围需为[-90,90]，经度范围需为[-180,180]，当前：纬度{lat}，经度{lon}"

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

    def _parse_image_meta(self, image_path: str) -> dict:
        """解析图片元数据（核心修复bytes属性错误）"""
        result = {
            "basic": {},
            "camera": {},  # 新增相机参数信息
            "xmp": {},     # 新增XMP数据
            "exif": {},
            "gps": {"lat": None, "lon": None, "str": "无GPS信息"},
            "error": None
        }
        try:
            # 基础文件信息
            file_size = os.path.getsize(image_path)
            result["basic"]["文件大小(KB)"] = round(file_size / 1024, 2)
            result["basic"]["文件大小(MB)"] = round(file_size / 1024 / 1024, 2)

            # 获取文件扩展名和类型
            _, file_ext = os.path.splitext(image_path)
            result["basic"]["文件格式"] = file_ext.upper()[1:] if file_ext else "未知"

            # 解析Exif（禁用详细模式，减少二进制数据）
            with open(image_path, 'rb') as f:
                exif_tags = exifread.process_file(f, details=False, stop_tag='GPS')

            # 提取基础图片信息（安全取值）
            if 'Image ImageWidth' in exif_tags:
                width_val = self._safe_get_exif_value(exif_tags['Image ImageWidth'])
                result["basic"]["宽度"] = f"{width_val} 像素"
            if 'Image ImageLength' in exif_tags:
                height_val = self._safe_get_exif_value(exif_tags['Image ImageLength'])
                result["basic"]["高度"] = f"{height_val} 像素"
            if 'Image Make' in exif_tags:
                make_val = self._safe_get_exif_value(exif_tags['Image Make'])
                result["basic"]["设备厂商"] = make_val
            if 'Image Model' in exif_tags:
                model_val = self._safe_get_exif_value(exif_tags['Image Model'])
                result["basic"]["设备型号"] = model_val
            if 'Image DateTime' in exif_tags:
                dt_val = self._safe_get_exif_value(exif_tags['Image DateTime'])
                result["basic"]["拍摄时间"] = dt_val

            # 提取相机参数信息
            if 'EXIF FNumber' in exif_tags:
                fnum_val = self._safe_get_exif_value(exif_tags['EXIF FNumber'])
                result["camera"]["光圈值"] = f"f/{fnum_val}"
            if 'EXIF ExposureTime' in exif_tags:
                exp_time_val = self._safe_get_exif_value(exif_tags['EXIF ExposureTime'])
                result["camera"]["快门速度"] = f"1/{exp_time_val}s" if '/' in exp_time_val else f"{exp_time_val}s"
            if 'EXIF ISOSpeedRatings' in exif_tags:
                iso_val = self._safe_get_exif_value(exif_tags['EXIF ISOSpeedRatings'])
                result["camera"]["ISO感光度"] = iso_val
            if 'EXIF FocalLength' in exif_tags:
                focal_val = self._safe_get_exif_value(exif_tags['EXIF FocalLength'])
                result["camera"]["焦距"] = f"{focal_val}mm"
            if 'EXIF Flash' in exif_tags:
                flash_val = self._safe_get_exif_value(exif_tags['EXIF Flash'])
                result["camera"]["闪光灯"] = "有" if flash_val != "0" else "无"
            if 'EXIF WhiteBalance' in exif_tags:
                wb_val = self._safe_get_exif_value(exif_tags['EXIF WhiteBalance'])
                result["camera"]["白平衡"] = "手动" if wb_val == "1" else "自动"
            if 'EXIF MeteringMode' in exif_tags:
                metering_val = self._safe_get_exif_value(exif_tags['EXIF MeteringMode'])
                metering_modes = {
                    "0": "未知", "1": "平均测光", "2": "中央重点平均测光", 
                    "3": "点测光", "4": "多点测光", "5": "图案测光", 
                    "6": "局部测光", "255": "其他"
                }
                result["camera"]["测光模式"] = metering_modes.get(metering_val, "未知")

            # 提取版权和作者信息
            if 'Image Artist' in exif_tags:
                artist_val = self._safe_get_exif_value(exif_tags['Image Artist'])
                result["exif"]["作者"] = artist_val
            if 'Image Copyright' in exif_tags:
                copyright_val = self._safe_get_exif_value(exif_tags['Image Copyright'])
                result["exif"]["版权"] = copyright_val
            if 'Image Software' in exif_tags:
                software_val = self._safe_get_exif_value(exif_tags['Image Software'])
                result["exif"]["编辑软件"] = software_val

            # 解析GPS
            lat, lon, gps_str = self._parse_gps_exifread(exif_tags)
            result["gps"]["lat"] = lat
            result["gps"]["lon"] = lon
            result["gps"]["str"] = gps_str

            # 解析XMP数据
            result["xmp"] = self._extract_xmp_data(image_path)

            # 提取其他Exif字段（过滤二进制数据，安全取值）
            exif_dict = {}
            for tag, value in exif_tags.items():
                # 跳过GPS相关（已单独解析）和已处理的基础信息
                if tag.startswith('GPS') or tag in [
                    'Image ImageWidth', 'Image ImageLength', 'Image Make', 
                    'Image Model', 'Image DateTime', 'EXIF FNumber', 
                    'EXIF ExposureTime', 'EXIF ISOSpeedRatings', 
                    'EXIF FocalLength', 'EXIF Flash', 'EXIF WhiteBalance',
                    'EXIF MeteringMode', 'Image Artist', 'Image Copyright', 
                    'Image Software'
                ]:
                    continue
                
                # 安全获取值，避免bytes错误
                val_str = self._safe_get_exif_value(value)
                
                # 过滤空值和过长的值
                if val_str and val_str != "None" and len(val_str) < 200:
                    exif_dict[tag.replace(' ', '_')] = val_str
            
            result["exif"].update(exif_dict)
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

            temp_file = tempfile.NamedTemporaryFile(suffix=".tmp", delete=False, encoding=None)
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
        return img_url

    async def process_metadata_analysis(self, event: AstrMessageEvent, image_path: str):
        """处理解析结果并发送"""
        try:
            meta = self._parse_image_meta(image_path)
            chain = []

            # 基础信息
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
            gps_lines = ["【GPS信息】", meta["gps"]["str"]]
            if meta["gps"]["lat"] and meta["gps"]["lon"]:
                addr_str = await self._gps_to_address(meta["gps"]["lat"], meta["gps"]["lon"])
                gps_lines.append(addr_str)
            chain.append(Comp.Plain("\n".join(gps_lines)))
            chain.append(Comp.Plain("‎\n‎"))

            # XMP信息
            if meta["xmp"] and not meta["xmp"].get("error"):  # 只有存在XMP数据且无错误时才显示
                xmp_lines = ["【XMP元数据】"]
                for k, v in meta["xmp"].items():
                    xmp_lines.append(f"{k}：{v}")
                chain.append(Comp.Plain("\n".join(xmp_lines)))
                chain.append(Comp.Plain("‎\n‎"))
            elif meta["xmp"] and meta["xmp"].get("error"):
                chain.append(Comp.Plain(f"【XMP解析错误】\n{meta['xmp']['error']}\n"))

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
                exif_lines.append("无")
            chain.append(Comp.Plain("\n".join(exif_lines)))

            # 错误信息
            if meta["error"]:
                chain.append(Comp.Plain(f"\n【解析提示】\n{meta['error']}"))

            await event.send(event.chain_result(chain))
        except Exception as e:
            logger.error(f"处理解析结果失败: {str(e)}")
            await event.send(event.plain_result(f"解析失败: {str(e)[:50]}..."))

    @filter.command("imgmeta", "图片元数据", "解析")
    async def imgmeta_handler(self, event: AstrMessageEvent, args=None):
        """主指令"""
        # 兼容不同版本的用户ID获取方式
        try:
            user_id = event.get_sender_id()
        except:
            user_id = str(event.user_id) if hasattr(event, 'user_id') else str(id(event))
        
        img_url = await self.extract_image_from_event(event)

        if img_url:
            temp_path = await self._download_image(img_url)
            if temp_path:
                await self.process_metadata_analysis(event, temp_path)
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
        self.timeout_tasks[user_id] = asyncio.create_task(self.timeout_check(user_id))
        await event.send(event.plain_result(self.prompt_send_image))

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
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
            await self.process_metadata_analysis(event, temp_path)
            try:
                os.unlink(temp_path)
            except:
                pass
        else:
            await event.send(event.plain_result("图片下载失败"))

    async def timeout_check(self, user_id: str):
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


def setup(context: Context):
    return ImageMetadataPlugin(context)