import json
import os
from contextlib import contextmanager
from threading import Lock

from conans.errors import ConanException
from conans.util.dates import timestamp_now
from conans.util.files import load, save
from conans.util.locks import SimpleLock
from conans.util.sha import sha256 as compute_sha256


class DownloadCache:
    """ The download cache has 3 folders
    - "s": SOURCE_BACKUP for the files.download(internet_url) backup sources feature
    - "c": CONAN_CACHE: for caching Conan packages artifacts
    - "locks": The LOCKS folder containing the file locks for concurrent access to the cache
    """
    _LOCKS = "locks"
    _SOURCE_BACKUP = "s"
    _CONAN_CACHE = "c"

    def __init__(self, path: str):
        self._path: str = path

    def source_path(self, sha256):
        return os.path.join(self._path, self._SOURCE_BACKUP, sha256)

    def cached_path(self, url):
        h = compute_sha256(url.encode())
        return os.path.join(self._path, self._CONAN_CACHE, h), h

    _thread_locks = {}  # Needs to be shared among all instances

    @contextmanager
    def lock(self, lock_id):
        lock = os.path.join(self._path, self._LOCKS, lock_id)
        with SimpleLock(lock):
            # Once the process has access, make sure multithread is locked too
            # as SimpleLock doesn't work multithread
            thread_lock = self._thread_locks.setdefault(lock, Lock())
            thread_lock.acquire()
            try:
                yield
            finally:
                thread_lock.release()

    def get_backup_sources_files_to_upload(self, excluded_urls, package_list=None):
        """ from a package_list of packages to upload, collect from the backup-sources cache
        the matching references to upload those backups too.
        If no package_list is passed, it gets all
        """
        path_backups = os.path.join(self._path, self._SOURCE_BACKUP)

        if not os.path.exists(path_backups):
            return []

        if excluded_urls is None:
            excluded_urls = []

        def has_excluded_urls(backup_urls):
            return all(any(url.startswith(excluded_url)
                           for excluded_url in excluded_urls)
                       for url in backup_urls)

        def should_upload_sources(package):
            return any(prev["upload"] for prev in package["revisions"].values())

        all_refs = set()
        if package_list is not None:
            for k, ref in package_list.refs().items():
                packages = ref.get("packages", {}).values()
                if ref.get("upload") or any(should_upload_sources(p) for p in packages):
                    all_refs.add(str(k))

        files_to_upload = []

        for path in os.listdir(path_backups):
            if not path.endswith(".json"):
                blob_path = os.path.join(path_backups, path)
                metadata_path = os.path.join(blob_path + ".json")
                if not os.path.exists(metadata_path):
                    raise ConanException(f"Missing metadata file for backup source {blob_path}")
                metadata = json.loads(load(metadata_path))
                refs = metadata["references"]
                # unknown entries are not uploaded at this moment unless no package_list is passed
                for ref, urls in refs.items():
                    if not has_excluded_urls(urls) and (package_list is None or ref in all_refs):
                        files_to_upload.append(metadata_path)
                        files_to_upload.append(blob_path)
                        break
        return files_to_upload

    @staticmethod
    def update_backup_sources_json(cached_path, conanfile, urls):
        """ create or update the sha256.json file with the references and new urls used
        """
        summary_path = cached_path + ".json"
        if os.path.exists(summary_path):
            summary = json.loads(load(summary_path))
        else:
            summary = {"references": {}, "timestamp": timestamp_now()}

        try:
            summary_key = str(conanfile.ref)
        except AttributeError:
            # The recipe path would be different between machines
            # So best we can do is to set this as unknown
            summary_key = "unknown"

        if not isinstance(urls, (list, tuple)):
            urls = [urls]
        existing_urls = summary["references"].setdefault(summary_key, [])
        existing_urls.extend(url for url in urls if url not in existing_urls)
        conanfile.output.verbose(f"Updating ${summary_path} summary file")
        summary_dump = json.dumps(summary)
        conanfile.output.debug(f"New summary: ${summary_dump}")
        save(summary_path, json.dumps(summary))
