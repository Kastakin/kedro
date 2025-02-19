"""This module provides ``kedro.config`` with the functionality to load one
or more configuration files of yaml or json type from specified paths through OmegaConf.
"""
import logging
from glob import iglob
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set  # noqa

from omegaconf import OmegaConf
from yaml.parser import ParserError
from yaml.scanner import ScannerError

from kedro.config import AbstractConfigLoader, MissingConfigException

_config_logger = logging.getLogger(__name__)


class OmegaConfLoader(AbstractConfigLoader):
    """Recursively scan directories (config paths) contained in ``conf_source`` for
    configuration files with a ``yaml``, ``yml`` or ``json`` extension, load and merge
    them through ``OmegaConf`` (https://omegaconf.readthedocs.io/)
    and return them in the form of a config dictionary.

    The first processed config path is the ``base`` directory inside
    ``conf_source``. The optional ``env`` argument can be used to specify a
    subdirectory of ``conf_source`` to process as a config path after ``base``.

    When the same top-level key appears in any two config files located in
    the same (sub)directory, a ``ValueError`` is raised.

    When the same key appears in any two config files located in different
    (sub)directories, the last processed config path takes precedence
    and overrides this key and any sub-keys.

    You can access the different configurations as follows:
    ::

        >>> import logging.config
        >>> from kedro.config import OmegaConfLoader
        >>> from kedro.framework.project import settings
        >>>
        >>> conf_path = str(project_path / settings.CONF_SOURCE)
        >>> conf_loader = OmegaConfLoader(conf_source=conf_path, env="local")
        >>>
        >>> conf_logging = conf_loader["logging"]
        >>> logging.config.dictConfig(conf_logging)  # set logging conf
        >>>
        >>> conf_catalog = conf_loader["catalog"]
        >>> conf_params = conf_loader["parameters"]

    ``OmegaConf`` supports variable interpolation in configuration
    https://omegaconf.readthedocs.io/en/2.2_branch/usage.html#merging-configurations. It is
    recommended to use this instead of yaml anchors with the ``OmegaConfLoader``.

    This version of the ``OmegaConfLoader`` does not support any of the built-in ``OmegaConf``
    resolvers. Support for resolvers might be added in future versions.

    To use this class, change the setting for the `CONFIG_LOADER_CLASS` constant
    in `settings.py`.

    Example:
    ::

        >>> # in settings.py
        >>> from kedro.config import OmegaConfLoader
        >>>
        >>> CONFIG_LOADER_CLASS = OmegaConfLoader

    """

    def __init__(
        self,
        conf_source: str,
        env: str = None,
        runtime_params: Dict[str, Any] = None,
        *,
        config_patterns: Dict[str, List[str]] = None,
        base_env: str = "base",
        default_run_env: str = "local",
    ):
        """Instantiates a ``OmegaConfLoader``.

        Args:
            conf_source: Path to use as root directory for loading configuration.
            env: Environment that will take precedence over base.
            runtime_params: Extra parameters passed to a Kedro run.
            config_patterns: Regex patterns that specify the naming convention for configuration
                files so they can be loaded. Can be customised by supplying config_patterns as
                in `CONFIG_LOADER_ARGS` in `settings.py`.
            base_env: Name of the base environment. Defaults to `"base"`.
                This is used in the `conf_paths` property method to construct
                the configuration paths.
            default_run_env: Name of the default run environment. Defaults to `"local"`.
                Can be overridden by supplying the `env` argument.
        """
        self.base_env = base_env
        self.default_run_env = default_run_env

        self.config_patterns = {
            "catalog": ["catalog*", "catalog*/**", "**/catalog*"],
            "parameters": ["parameters*", "parameters*/**", "**/parameters*"],
            "credentials": ["credentials*", "credentials*/**", "**/credentials*"],
            "logging": ["logging*", "logging*/**", "**/logging*"],
        }
        self.config_patterns.update(config_patterns or {})

        # In the first iteration of the OmegaConfLoader we'll keep the resolver turned-off.
        # It's easier to introduce them step by step, but removing them would be a breaking change.
        self._clear_omegaconf_resolvers()

        super().__init__(
            conf_source=conf_source,
            env=env,
            runtime_params=runtime_params,
        )

    def __getitem__(self, key) -> Dict[str, Any]:
        """Get configuration files by key, load and merge them, and
        return them in the form of a config dictionary.

        Args:
            key: Key of the configuration type to fetch.

        Raises:
            KeyError: If key provided isn't present in the config_patterns of this
               OmegaConfLoader instance.
            MissingConfigException: If no configuration files exist matching the patterns
                mapped to the provided key.

        Returns:
            Dict[str, Any]:  A Python dictionary with the combined
               configuration from all configuration files.
        """

        # Allow bypassing of loading config from patterns if a key and value have been set
        # explicitly on the ``OmegaConfLoader`` instance.
        if key in self:
            return super().__getitem__(key)

        if key not in self.config_patterns:
            raise KeyError(
                f"No config patterns were found for '{key}' in your config loader"
            )
        patterns = [*self.config_patterns[key]]

        # Load base env config
        base_path = str(Path(self.conf_source) / self.base_env)
        base_config = self.load_and_merge_dir_config(base_path, patterns)
        config = base_config

        # Load chosen env config
        run_env = self.env or self.default_run_env
        env_path = str(Path(self.conf_source) / run_env)
        env_config = self.load_and_merge_dir_config(env_path, patterns)

        # Destructively merge the two env dirs. The chosen env will override base.
        common_keys = config.keys() & env_config.keys()
        if common_keys:
            sorted_keys = ", ".join(sorted(common_keys))
            msg = (
                "Config from path '%s' will override the following "
                "existing top-level config keys: %s"
            )
            _config_logger.debug(msg, env_path, sorted_keys)

        config.update(env_config)

        if not config:
            raise MissingConfigException(
                f"No files of YAML or JSON format found in {base_path} or {env_path} matching"
                f" the glob pattern(s): {[*self.config_patterns[key]]}"
            )
        return config

    def __repr__(self):  # pragma: no cover
        return (
            f"OmegaConfLoader(conf_source={self.conf_source}, env={self.env}, "
            f"config_patterns={self.config_patterns})"
        )

    def load_and_merge_dir_config(self, conf_path: str, patterns: Iterable[str]):
        """Recursively load and merge all configuration files in a directory using OmegaConf,
        which satisfy a given list of glob patterns from a specific path.

        Args:
            conf_path: Path to configuration directory.
            patterns: List of glob patterns to match the filenames against.

        Raises:
            MissingConfigException: If configuration path doesn't exist or isn't valid.
            ValueError: If two or more configuration files contain the same key(s).
            ParserError: If config file contains invalid YAML or JSON syntax.

        Returns:
            Resulting configuration dictionary.

        """
        if not Path(conf_path).is_dir():
            raise MissingConfigException(
                f"Given configuration path either does not exist "
                f"or is not a valid directory: {conf_path}"
            )

        paths = [
            Path(each).resolve()
            for pattern in patterns
            for each in iglob(f"{str(conf_path)}/{pattern}", recursive=True)
        ]
        deduplicated_paths = set(paths)
        config_files_filtered = [
            path for path in deduplicated_paths if self._is_valid_config_path(path)
        ]

        config_per_file = {}

        for config_filepath in config_files_filtered:
            try:
                config = OmegaConf.load(config_filepath)
                config_per_file[config_filepath] = config
            except (ParserError, ScannerError) as exc:
                line = exc.problem_mark.line  # type: ignore
                cursor = exc.problem_mark.column  # type: ignore
                raise ParserError(
                    f"Invalid YAML or JSON file {config_filepath}, unable to read line {line}, "
                    f"position {cursor}."
                ) from exc

        seen_file_to_keys = {
            file: set(config.keys()) for file, config in config_per_file.items()
        }
        aggregate_config = config_per_file.values()
        self._check_duplicates(seen_file_to_keys)

        if not aggregate_config:
            return {}
        if len(aggregate_config) == 1:
            return list(aggregate_config)[0]
        return dict(OmegaConf.merge(*aggregate_config))

    @staticmethod
    def _is_valid_config_path(path):
        """Check if given path is a file path and file type is yaml or json."""
        return path.is_file() and path.suffix in [".yml", ".yaml", ".json"]

    @staticmethod
    def _check_duplicates(seen_files_to_keys: Dict[Path, Set[Any]]):
        duplicates = []

        filepaths = list(seen_files_to_keys.keys())
        for i, filepath1 in enumerate(filepaths, 1):
            config1 = seen_files_to_keys[filepath1]
            for filepath2 in filepaths[i:]:
                config2 = seen_files_to_keys[filepath2]

                overlapping_keys = config1 & config2

                if overlapping_keys:
                    sorted_keys = ", ".join(sorted(overlapping_keys))
                    if len(sorted_keys) > 100:
                        sorted_keys = sorted_keys[:100] + "..."
                    duplicates.append(
                        f"Duplicate keys found in {filepath1} and {filepath2}: {sorted_keys}"
                    )

        if duplicates:
            dup_str = "\n".join(duplicates)
            raise ValueError(f"{dup_str}")

    @staticmethod
    def _clear_omegaconf_resolvers():
        """Clear the built-in OmegaConf resolvers."""
        OmegaConf.clear_resolver("oc.env")
        OmegaConf.clear_resolver("oc.create")
        OmegaConf.clear_resolver("oc.deprecated")
        OmegaConf.clear_resolver("oc.decode")
        OmegaConf.clear_resolver("oc.select")
        OmegaConf.clear_resolver("oc.dict.keys")
        OmegaConf.clear_resolver("oc.dict.values")
