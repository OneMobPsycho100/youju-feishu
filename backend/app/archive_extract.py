"""在独立进程中校验、解包用户原件，临时文件只使用程序分配的编号。"""
from dataclasses import dataclass
import json
import lzma
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import struct
import sys
from unicodedata import normalize
import zipfile
import zlib

from py7zr import Py7zIO, SevenZipFile, WriterFactory
from py7zr.exceptions import (AbsolutePathError, ArchiveError, DecompressionBombError,
                              PasswordRequired, UnsupportedCompressionMethodError)

from .file_types import ARCHIVE_MIMES, detect_file_type


class InvalidArchive(ValueError):
    """可在审批页公开显示的解包失败原因。"""


TOO_LARGE = "压缩包解开后超过文件数量或大小限制，请拆分材料"


@dataclass(frozen=True)
class Member:
    """容器内原始标识、归一化展示路径与声明大小，目录只参与安全检查。"""

    name: str
    path: str
    size: int
    directory: bool


def member_path(name: str) -> str:
    """允许普通子目录。拒绝绝对路径、磁盘标识、回退段和设备文件名。"""
    name = normalize("NFC", name.replace("\\", "/"))
    path = PurePosixPath(name)
    if (len(name) > 512 or path.is_absolute() or not path.parts or ":" in name or ".." in path.parts
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
            or any(PureWindowsPath(part).is_reserved() for part in path.parts)):
        raise InvalidArchive("压缩包含不安全的文件路径")
    return path.as_posix()


def zip_member_name(info: zipfile.ZipInfo) -> str:
    """UTF-8 标记优先。旧 Windows ZIP 的中文名称按 GB18030 还原。"""
    name = info.orig_filename
    if not info.flag_bits & 0x800 and not name.isascii():
        try:
            decoded = name.encode("cp437").decode("gb18030")
        except UnicodeError:
            return name
        if any("\u4e00" <= char <= "\u9fff" for char in decoded):
            return decoded
    return name


def checked_members(entries: list[Member], max_count: int, max_total: int, max_file: int) -> list[Member]:
    """先核对完整清单再读取字节，单个不合格成员会阻止整包发布。"""
    paths = {item.path.casefold(): item for item in entries}
    if len(paths) != len(entries) or any(
        parent.as_posix() in paths and not paths[parent.as_posix()].directory
        for path in paths for parent in PurePosixPath(path).parents
    ):
        raise InvalidArchive("压缩包内存在重复或冲突的文件路径")
    members = [item for item in entries if not item.directory]
    if not members:
        raise InvalidArchive("压缩包内没有可读取的文件")
    if (len(members) > max_count or sum(item.size for item in members) > max_total
            or any(not 0 <= item.size <= max_file for item in members)):
        raise InvalidArchive(TOO_LARGE)
    return members


def extract_zip(source: Path, work: Path, limits: tuple[int, int, int]) -> list[Member]:
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        if len(entries) > 500:
            raise InvalidArchive(TOO_LARGE)
        members = []
        for info in entries:
            if info.flag_bits & 1:
                raise InvalidArchive("压缩包已加密，请上传未加密的文件")
            kind = stat.S_IFMT(info.external_attr >> 16)
            if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise InvalidArchive("压缩包包含链接或特殊文件")
            directory = info.is_dir() or kind == stat.S_IFDIR
            name = zip_member_name(info)
            # 部分工具会写入表示包根目录的 ./ 项。
            if directory and name in {"./", "."}:
                continue
            members.append(Member(info.filename, member_path(name), info.file_size, directory))
        files = checked_members(members, *limits)
        for index, item in enumerate(files):
            with archive.open(item.name) as src, (work / str(index)).open("xb") as target:
                remaining = item.size
                while block := src.read(64 * 1024):
                    remaining -= len(block)
                    if remaining < 0:
                        raise InvalidArchive(TOO_LARGE)
                    target.write(block)
                if remaining:
                    raise InvalidArchive("压缩包成员大小不符，请重新打包")
        return files


class BoundedWriter(Py7zIO):
    """py7zr 只得到受限的写入对象，解压库不会用成员路径操作文件系统。"""

    def __init__(self, path: Path, limit: int):
        self.stream = path.open("x+b")
        self.limit = limit
        self.written = 0

    def write(self, data: bytes | bytearray) -> int:
        self.written += len(data)
        if self.written > self.limit:
            raise InvalidArchive(TOO_LARGE)
        return self.stream.write(data)

    def read(self, size: int | None = None) -> bytes:
        return self.stream.read(-1 if size is None else size)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self.stream.seek(offset, whence)

    def flush(self) -> None:
        self.stream.flush()

    def size(self) -> int:
        return self.written

    def close(self) -> None:
        self.stream.close()


class NumberedFiles(WriterFactory):
    def __init__(self, work: Path, members: list[Member]):
        self.work = work
        self.targets = {item.path: (index, item.size) for index, item in enumerate(members)}
        self.opened: list[BoundedWriter] = []

    def create(self, filename: str) -> Py7zIO:
        relative = Path(filename).relative_to(self.work).as_posix()
        index, size = self.targets[member_path(relative)]
        writer = BoundedWriter(self.work / str(index), size)
        self.opened.append(writer)
        return writer


def extract_7z(source: Path, work: Path, limits: tuple[int, int, int]) -> list[Member]:
    # 传入文件对象使固实包走顺序解压，输出只允许写入编号文件。
    with source.open("rb") as stream, SevenZipFile(stream, "r", max_extract_size=limits[1]) as archive:
        if archive.needs_password():
            raise InvalidArchive("压缩包已加密，请上传未加密的文件")
        entries = archive.list()
        if len(entries) > 500:
            raise InvalidArchive(TOO_LARGE)
        members = []
        for info in entries:
            if info.is_symlink or not (info.is_directory or info.is_file):
                raise InvalidArchive("压缩包包含链接或特殊文件")
            if info.is_directory and info.filename in {".", "./"}:
                continue
            members.append(Member(info.filename, member_path(info.filename), info.uncompressed, info.is_directory))
        files = checked_members(members, *limits)
        factory = NumberedFiles(work, files)
        try:
            archive.extract(path=work, targets=[item.name for item in files], factory=factory)
        finally:
            for writer in factory.opened:
                writer.close()
        return files


def extract(source: Path, work: Path, suffix: str, limits: tuple[int, int, int]) -> list[str]:
    """完整验证大小和成员类型后返回路径，调用方才会保存附件。"""
    unpack = extract_zip if suffix == ".zip" else extract_7z
    members = unpack(source, work, limits)
    for index, item in enumerate(members):
        path = work / str(index)
        if not path.is_file() or path.stat().st_size != item.size:
            raise InvalidArchive("压缩包成员大小不符，请重新打包")
        if detect_file_type(path.read_bytes(), item.path)[1] in ARCHIVE_MIMES:
            raise InvalidArchive("压缩包内含嵌套压缩包，请展开后重新上传")
    return [item.path for item in members]


def main() -> None:
    source, work, suffix, count, total, single = sys.argv[1:]
    if sys.platform == "linux":
        # 防止 7z 字典或恶意文件目录先于业务字节计数耗尽服务内存。
        import resource

        limit = 512 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    try:
        paths = extract(Path(source), Path(work), suffix, (int(count), int(total), int(single)))
        result = {"paths": paths}
    except InvalidArchive as exc:
        result = {"error": str(exc)}
    except PasswordRequired:
        result = {"error": "压缩包已加密，请上传未加密的文件"}
    except (MemoryError, DecompressionBombError):
        result = {"error": TOO_LARGE}
    except (NotImplementedError, UnsupportedCompressionMethodError):
        result = {"error": "压缩方法或分卷格式暂不支持，请重新打包为单个 ZIP 或 7z"}
    except AbsolutePathError:
        result = {"error": "压缩包含不安全的文件路径"}
    except OSError as exc:
        # 损坏的 BZIP2 数据可能以无系统错误码的 OSError 结束。磁盘故障交给任务重试。
        if exc.errno is not None:
            raise
        result = {"error": "压缩包损坏或不完整，请重新打包"}
    except (ArchiveError, zipfile.BadZipFile, struct.error, lzma.LZMAError, zlib.error,
            EOFError, IndexError, KeyError, OverflowError, RuntimeError, ValueError, AssertionError):
        result = {"error": "压缩包损坏或不完整，请重新打包"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
