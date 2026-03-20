import platform
import subprocess
import os

def detect_os():
    """检测操作系统类型"""
    system = platform.system().lower()
    if system == "windows":
        print("Windows暂不支持！")
        return "windows"
    elif system == "darwin":
        return "macos"
    elif system == "linux":
        return "linux"
    else:
        return system

def is_exiftool_installed():
    """检查exiftool是否已安装"""
    try:
        result = subprocess.run(['exiftool', '-ver'], capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False

def install_exiftool_macos():
    """macOS安装exiftool"""    
    try:
        result = subprocess.run(['brew', '--version'], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            print("Homebrew未安装")
            return False
        
        result = subprocess.run(['brew', 'install', 'exiftool'], capture_output=True, text=True, timeout=60)
        
        if result.returncode == 0:
            return True
        else:
            print(f"exiftool安装失败: {result.stderr}")
            return False
    except Exception as e:
        print(f"macOS安装过程中出现错误: {str(e)}")
        return False

def install_exiftool_linux():
    """Linux安装exiftool"""
    
    try:
        # 尝试使用不同的包管理器
        package_managers = [
            ('apt-get', ['apt-get', 'install', '-y', 'libimage-exiftool-perl']),
            ('yum', ['yum', 'install', '-y', 'perl-Image-ExifTool']),
            ('dnf', ['dnf', 'install', '-y', 'perl-Image-ExifTool']),
            ('pacman', ['pacman', '-S', '--noconfirm', 'perl-image-exiftool']),
        ]
        
        installed = False
        
        for pkg_manager, cmd in package_managers:
            try:
                # 检查包管理器是否存在
                subprocess.run([pkg_manager, '--version'], capture_output=True, check=True)
                print(f"检测到{pkg_manager}包管理器，正在安装exiftool...")
                
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                
                if result.returncode == 0:
                    installed = True
                    break
                else:
                    print(f"{pkg_manager}安装失败: {result.stderr}")
                    
            except subprocess.CalledProcessError:
                print(f"系统未安装{pkg_manager}包管理器，尝试下一个...")
                continue
            except subprocess.TimeoutExpired:
                print(f"{pkg_manager}安装超时，尝试下一个...")
                continue
        
        if not installed:
            print("\n无法通过包管理器安装exiftool，请手动安装：")
            return False
            
        return True
        
    except Exception as e:
        print(f"Linux安装过程中出现错误: {str(e)}")
        return False
def write_install_flag():
    """写入安装标记"""
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        install_txt_path = os.path.join(script_dir, "install.txt")
        with open(install_txt_path, "w", encoding="utf-8") as f:
            f.write("done")
        return True
    except Exception as e:
        print(f"创建标记失败: {e}")
        return False

def main():
    """主安装函数"""
    # 检查是否已经安装
    if is_exiftool_installed():
        try:
            result = subprocess.run(['exiftool', '-ver'], capture_output=True, text=True, timeout=10)
            version = result.stdout.strip()
            print(f"当前版本: {version}")
        except:
            pass
        
        # 创建安装完成标记文件
        try:
            write_install_flag()
            print("安装标记文件已创建: install.txt")
        except Exception as e:
            print(f"创建标记文件失败: {str(e)}")
        
        return True
    
    # 根据操作系统选择安装方法
    os_type = detect_os()    
    success = False
    if os_type == "macos":
        success = install_exiftool_macos()
    elif os_type == "linux":
        success = install_exiftool_linux()
    else:
        return False
    
    if success:
        print("\nexiftool安装完成！")
        
        # 再次验证安装
        if is_exiftool_installed():
            try:
                result = subprocess.run(['exiftool', '-ver'], capture_output=True, text=True, timeout=10)
                version = result.stdout.strip()
                print(f"验证成功，版本: {version}")
                
                # 创建安装完成标记文件
                try:
                    write_install_flag()
                    print("安装标记文件已创建: install.txt")
                except Exception as e:
                    print(f"创建标记文件失败: {str(e)}")
            except:
                print("安装完成但验证时出现问题")
        else:
            print("安装完成但验证失败，请检查安装是否正确")
        
        return True
    else:
        print("\nexiftool安装失败！")
        return False

if __name__ == "__main__":
    main()