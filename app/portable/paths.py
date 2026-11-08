"""Separate immutable bundle resources from an explicit writable profile."""
from dataclasses import dataclass
from pathlib import Path
import sys
import tempfile


class PortableError(RuntimeError):
    """A safe, actionable startup message suitable for the user's console."""


@dataclass(frozen=True)
class PortablePaths:
    application: Path
    resources: Path
    profile: Path

    @classmethod
    def discover(cls, *, executable=None, resources=None, data_dir=None, frozen=None):
        frozen = getattr(sys, 'frozen', False) if frozen is None else frozen
        application = Path(executable or sys.executable).resolve().parent if frozen else Path(__file__).resolve().parents[2]
        resources = Path(resources or getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2])).resolve()
        if not frozen and data_dir is None:
            raise PortableError('Для запуска из исходников укажите отдельный профиль: --data-dir <каталог>.')
        profile = Path(data_dir).expanduser() if data_dir is not None else application
        if not profile.is_absolute():
            profile = application / profile
        profile = profile.resolve()
        if profile == resources or resources in profile.parents or any(p.casefold() == '_internal' for p in profile.parts):
            raise PortableError('Каталог данных не может находиться внутри ресурсов или _internal. Выберите --data-dir вне программных ресурсов.')
        return cls(application, resources, profile)

    @property
    def data(self): return self.profile / 'data'

    @property
    def database(self): return self.data / 'construction_progress.sqlite3'

    @property
    def backups(self): return self.profile / 'backups'

    @property
    def exports(self): return self.profile / 'exports'

    @property
    def logs(self): return self.profile / 'logs'

    def ensure_writable(self):
        try:
            for directory in (self.profile, self.data, self.backups, self.exports, self.logs):
                if directory.resolve() != self.profile and self.profile not in directory.resolve().parents:
                    raise PortableError('Каталог профиля ссылается за его пределы. Проверьте ссылки и --data-dir.')
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                with tempfile.NamedTemporaryFile(prefix='.write-check-', dir=directory):
                    pass
            if self.database.is_symlink():
                raise PortableError('База не должна быть символической ссылкой. Перенесите закрытую базу в каталог data выбранного профиля.')
            if self.database.exists():
                with self.database.open('r+b'):
                    pass
        except OSError as exc:
            raise PortableError('Нет доступа для записи в профиль {}. Распакуйте приложение в личный каталог или укажите --data-dir. Права администратора не требуются.'.format(self.profile)) from exc
