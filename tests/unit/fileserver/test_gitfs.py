"""
    :codeauthor: Erik Johnson <erik@saltstack.com>
"""

import errno
import logging
import os
import shutil
import stat
import tempfile
import textwrap

import pytest

import salt.ext.tornado.ioloop
import salt.fileserver.gitfs as gitfs
import salt.utils.files
import salt.utils.gitfs
import salt.utils.platform
import salt.utils.win_functions
import salt.utils.yaml
from salt.utils.gitfs import (
    GITPYTHON_MINVER,
    GITPYTHON_VERSION,
    LIBGIT2_MINVER,
    LIBGIT2_VERSION,
    PYGIT2_MINVER,
    PYGIT2_VERSION,
)
from tests.support.helpers import patched_environ
from tests.support.mixins import LoaderModuleMockMixin
from tests.support.mock import patch
from tests.support.runtests import RUNTIME_VARS
from tests.support.unit import TestCase, skipIf

try:
    import pwd  # pylint: disable=unused-import
except ImportError:
    pass


try:
    import git

    # We still need to use GitPython here for temp repo setup, so we do need to
    # actually import it. But we don't need import pygit2 in this module, we
    # can just use the LooseVersion instances imported along with
    # salt.utils.gitfs to check if we have a compatible version.
    HAS_GITPYTHON = GITPYTHON_VERSION >= GITPYTHON_MINVER
except (ImportError, AttributeError):
    HAS_GITPYTHON = False

try:
    HAS_PYGIT2 = PYGIT2_VERSION >= PYGIT2_MINVER and LIBGIT2_VERSION >= LIBGIT2_MINVER
except AttributeError:
    HAS_PYGIT2 = False

log = logging.getLogger(__name__)

UNICODE_FILENAME = "питон.txt"
UNICODE_DIRNAME = UNICODE_ENVNAME = "соль"
TAG_NAME = "mytag"


def _rmtree_error(func, path, excinfo):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _clear_instance_map():
    try:
        del salt.utils.gitfs.GitFS.instance_map[
            salt.ext.tornado.ioloop.IOLoop.current()
        ]
    except KeyError:
        pass


@skipIf(not HAS_GITPYTHON, "GitPython >= {} required".format(GITPYTHON_MINVER))
class GitfsConfigTestCase(TestCase, LoaderModuleMockMixin):
    def setup_loader_modules(self):
        opts = {
            "sock_dir": self.tmp_sock_dir,
            "gitfs_remotes": ["file://" + self.tmp_repo_dir],
            "gitfs_root": "",
            "fileserver_backend": ["gitfs"],
            "gitfs_base": "master",
            "gitfs_fallback": "",
            "fileserver_events": True,
            "transport": "zeromq",
            "gitfs_mountpoint": "",
            "gitfs_saltenv": [],
            "gitfs_saltenv_whitelist": [],
            "gitfs_saltenv_blacklist": [],
            "gitfs_user": "",
            "gitfs_password": "",
            "gitfs_insecure_auth": False,
            "gitfs_privkey": "",
            "gitfs_pubkey": "",
            "gitfs_passphrase": "",
            "gitfs_refspecs": [
                "+refs/heads/*:refs/remotes/origin/*",
                "+refs/tags/*:refs/tags/*",
            ],
            "gitfs_ssl_verify": True,
            "gitfs_disable_saltenv_mapping": False,
            "gitfs_ref_types": ["branch", "tag", "sha"],
            "gitfs_update_interval": 60,
            "__role": "master",
        }
        opts["cachedir"] = self.tmp_cachedir
        opts["sock_dir"] = self.tmp_sock_dir
        return {gitfs: {"__opts__": opts}}

    @classmethod
    def setUpClass(cls):
        # Clear the instance map so that we make sure to create a new instance
        # for this test class.
        _clear_instance_map()
        cls.tmp_repo_dir = os.path.join(RUNTIME_VARS.TMP, "gitfs_root")
        if salt.utils.platform.is_windows():
            cls.tmp_repo_dir = cls.tmp_repo_dir.replace("\\", "/")
        cls.tmp_cachedir = tempfile.mkdtemp(dir=RUNTIME_VARS.TMP)
        cls.tmp_sock_dir = tempfile.mkdtemp(dir=RUNTIME_VARS.TMP)

    @classmethod
    def tearDownClass(cls):
        """
        Remove the temporary git repository and gitfs cache directory to ensure
        a clean environment for the other test class(es).
        """
        for path in (cls.tmp_cachedir, cls.tmp_sock_dir):
            try:
                shutil.rmtree(path, onerror=_rmtree_error)
            except OSError as exc:
                if exc.errno == errno.EACCES:
                    log.error("Access error removeing file %s", path)
                    continue
                if exc.errno != errno.EEXIST:
                    raise

    def test_per_saltenv_config(self):
        opts_override = textwrap.dedent(
            """
            gitfs_root: salt

            gitfs_saltenv:
              - baz:
                # when loaded, the "salt://" prefix will be removed
                - mountpoint: salt://baz_mountpoint
                - ref: baz_branch
                - root: baz_root

            gitfs_remotes:

              - file://{0}tmp/repo1:
                - saltenv:
                  - foo:
                    - ref: foo_branch
                    - root: foo_root

              - file://{0}tmp/repo2:
                - mountpoint: repo2
                - saltenv:
                  - baz:
                    - mountpoint: abc
        """.format(
                "/" if salt.utils.platform.is_windows() else ""
            )
        )
        with patch.dict(gitfs.__opts__, salt.utils.yaml.safe_load(opts_override)):
            git_fs = salt.utils.gitfs.GitFS(
                gitfs.__opts__,
                gitfs.__opts__["gitfs_remotes"],
                per_remote_overrides=gitfs.PER_REMOTE_OVERRIDES,
                per_remote_only=gitfs.PER_REMOTE_ONLY,
            )

        # repo1 (branch: foo)
        # The mountpoint should take the default (from gitfs_mountpoint), while
        # ref and root should take the per-saltenv params.
        self.assertEqual(git_fs.remotes[0].mountpoint("foo"), "")
        self.assertEqual(git_fs.remotes[0].ref("foo"), "foo_branch")
        self.assertEqual(git_fs.remotes[0].root("foo"), "foo_root")

        # repo1 (branch: bar)
        # The 'bar' branch does not have a per-saltenv configuration set, so
        # each of the below values should fall back to global values.
        self.assertEqual(git_fs.remotes[0].mountpoint("bar"), "")
        self.assertEqual(git_fs.remotes[0].ref("bar"), "bar")
        self.assertEqual(git_fs.remotes[0].root("bar"), "salt")

        # repo1 (branch: baz)
        # The 'baz' branch does not have a per-saltenv configuration set, but
        # it is defined in the gitfs_saltenv parameter, so the values
        # from that parameter should be returned.
        self.assertEqual(git_fs.remotes[0].mountpoint("baz"), "baz_mountpoint")
        self.assertEqual(git_fs.remotes[0].ref("baz"), "baz_branch")
        self.assertEqual(git_fs.remotes[0].root("baz"), "baz_root")

        # repo2 (branch: foo)
        # The mountpoint should take the per-remote mountpoint value of
        # 'repo2', while ref and root should fall back to global values.
        self.assertEqual(git_fs.remotes[1].mountpoint("foo"), "repo2")
        self.assertEqual(git_fs.remotes[1].ref("foo"), "foo")
        self.assertEqual(git_fs.remotes[1].root("foo"), "salt")

        # repo2 (branch: bar)
        # The 'bar' branch does not have a per-saltenv configuration set, so
        # the mountpoint should take the per-remote mountpoint value of
        # 'repo2', while ref and root should fall back to global values.
        self.assertEqual(git_fs.remotes[1].mountpoint("bar"), "repo2")
        self.assertEqual(git_fs.remotes[1].ref("bar"), "bar")
        self.assertEqual(git_fs.remotes[1].root("bar"), "salt")

        # repo2 (branch: baz)
        # The 'baz' branch has the mountpoint configured as a per-saltenv
        # parameter. The other two should take the values defined in
        # gitfs_saltenv.
        self.assertEqual(git_fs.remotes[1].mountpoint("baz"), "abc")
        self.assertEqual(git_fs.remotes[1].ref("baz"), "baz_branch")
        self.assertEqual(git_fs.remotes[1].root("baz"), "baz_root")


LOAD = {"saltenv": "base"}


class GitFSTestFuncs:
    """
    These are where the tests go, so that they can be run using both GitPython
    and pygit2.

    NOTE: The gitfs.update() has to happen AFTER the setUp is called. This is
    because running it inside the setUp will spawn a new singleton, which means
    that tests which need to mock the __opts__ will be too late; the setUp will
    have created a new singleton that will bypass our mocking. To ensure that
    our tests are reliable and correct, we want to make sure that each test
    uses a new gitfs object, allowing different manipulations of the opts to be
    tested.

    Therefore, keep the following in mind:

    1. Each test needs to call gitfs.update() *after* any patching, and
       *before* calling the function being tested.
    2. Do *NOT* move the gitfs.update() into the setUp.
    """

    @pytest.mark.slow_test
    def test_file_list(self):
        gitfs.update()
        ret = gitfs.file_list(LOAD)
        self.assertIn("testfile", ret)
        self.assertIn(UNICODE_FILENAME, ret)
        # This function does not use os.sep, the Salt fileserver uses the
        # forward slash, hence it being explicitly used to join here.
        self.assertIn("/".join((UNICODE_DIRNAME, "foo.txt")), ret)

    @pytest.mark.slow_test
    def test_dir_list(self):
        gitfs.update()
        ret = gitfs.dir_list(LOAD)
        self.assertIn("grail", ret)
        self.assertIn(UNICODE_DIRNAME, ret)

    def test_find_and_serve_file(self):
        with patch.dict(gitfs.__opts__, {"file_buffer_size": 262144}):
            gitfs.update()

            # find_file
            ret = gitfs.find_file("testfile")
            self.assertEqual("testfile", ret["rel"])

            full_path_to_file = salt.utils.path.join(
                gitfs.__opts__["cachedir"], "gitfs", "refs", "base", "testfile"
            )
            self.assertEqual(full_path_to_file, ret["path"])

            # serve_file
            load = {"saltenv": "base", "path": full_path_to_file, "loc": 0}
            fnd = {"path": full_path_to_file, "rel": "testfile"}
            ret = gitfs.serve_file(load, fnd)

            with salt.utils.files.fopen(
                os.path.join(RUNTIME_VARS.BASE_FILES, "testfile"), "r"
            ) as fp_:  # NB: Why not 'rb'?
                data = fp_.read()

            self.assertDictEqual(ret, {"data": data, "dest": "testfile"})

    def test_file_list_fallback(self):
        with patch.dict(gitfs.__opts__, {"gitfs_fallback": "master"}):
            gitfs.update()
            ret = gitfs.file_list({"saltenv": "notexisting"})
            self.assertIn("testfile", ret)
            self.assertIn(UNICODE_FILENAME, ret)
            # This function does not use os.sep, the Salt fileserver uses the
            # forward slash, hence it being explicitly used to join here.
            self.assertIn("/".join((UNICODE_DIRNAME, "foo.txt")), ret)

    def test_dir_list_fallback(self):
        with patch.dict(gitfs.__opts__, {"gitfs_fallback": "master"}):
            gitfs.update()
            ret = gitfs.dir_list({"saltenv": "notexisting"})
            self.assertIn("grail", ret)
            self.assertIn(UNICODE_DIRNAME, ret)

    def test_find_and_serve_file_fallback(self):
        with patch.dict(
            gitfs.__opts__, {"file_buffer_size": 262144, "gitfs_fallback": "master"}
        ):
            gitfs.update()

            # find_file
            ret = gitfs.find_file("testfile", tgt_env="notexisting")
            self.assertEqual("testfile", ret["rel"])

            full_path_to_file = salt.utils.path.join(
                gitfs.__opts__["cachedir"], "gitfs", "refs", "notexisting", "testfile"
            )
            self.assertEqual(full_path_to_file, ret["path"])

            # serve_file
            load = {"saltenv": "notexisting", "path": full_path_to_file, "loc": 0}
            fnd = {"path": full_path_to_file, "rel": "testfile"}
            ret = gitfs.serve_file(load, fnd)

            with salt.utils.files.fopen(
                os.path.join(RUNTIME_VARS.BASE_FILES, "testfile"), "r"
            ) as fp_:  # NB: Why not 'rb'?
                data = fp_.read()

            self.assertDictEqual(ret, {"data": data, "dest": "testfile"})

    @pytest.mark.slow_test
    def test_envs(self):
        gitfs.update()
        ret = gitfs.envs(ignore_cache=True)
        self.assertIn("base", ret)
        self.assertIn(UNICODE_ENVNAME, ret)
        self.assertIn(TAG_NAME, ret)

    @pytest.mark.slow_test
    def test_ref_types_global(self):
        """
        Test the global gitfs_ref_types config option
        """
        with patch.dict(gitfs.__opts__, {"gitfs_ref_types": ["branch"]}):
            gitfs.update()
            ret = gitfs.envs(ignore_cache=True)
            # Since we are restricting to branches only, the tag should not
            # appear in the envs list.
            self.assertIn("base", ret)
            self.assertIn(UNICODE_ENVNAME, ret)
            self.assertNotIn(TAG_NAME, ret)

    @pytest.mark.slow_test
    def test_ref_types_per_remote(self):
        """
        Test the per_remote ref_types config option, using a different
        ref_types setting than the global test.
        """
        remotes = [{"file://" + self.tmp_repo_dir: [{"ref_types": ["tag"]}]}]
        with patch.dict(gitfs.__opts__, {"gitfs_remotes": remotes}):
            gitfs.update()
            ret = gitfs.envs(ignore_cache=True)
            # Since we are restricting to tags only, the tag should appear in
            # the envs list, but the branches should not.
            self.assertNotIn("base", ret)
            self.assertNotIn(UNICODE_ENVNAME, ret)
            self.assertIn(TAG_NAME, ret)

    @pytest.mark.slow_test
    def test_disable_saltenv_mapping_global_with_mapping_defined_globally(self):
        """
        Test the global gitfs_disable_saltenv_mapping config option, combined
        with the per-saltenv mapping being defined in the global gitfs_saltenv
        option.
        """
        opts = salt.utils.yaml.safe_load(
            textwrap.dedent(
                """\
            gitfs_disable_saltenv_mapping: True
            gitfs_saltenv:
              - foo:
                - ref: somebranch
            """
            )
        )
        with patch.dict(gitfs.__opts__, opts):
            gitfs.update()
            ret = gitfs.envs(ignore_cache=True)
            # Since we are restricting to tags only, the tag should appear in
            # the envs list, but the branches should not.
            self.assertEqual(ret, ["base", "foo"])

    @pytest.mark.slow_test
    def test_saltenv_blacklist(self):
        """
        test saltenv_blacklist
        """
        opts = salt.utils.yaml.safe_load(
            textwrap.dedent(
                """\
            gitfs_saltenv_blacklist: base
            """
            )
        )
        with patch.dict(gitfs.__opts__, opts):
            gitfs.update()
            ret = gitfs.envs(ignore_cache=True)
            assert "base" not in ret
            assert UNICODE_ENVNAME in ret
            assert "mytag" in ret

    @pytest.mark.slow_test
    def test_saltenv_whitelist(self):
        """
        test saltenv_whitelist
        """
        opts = salt.utils.yaml.safe_load(
            textwrap.dedent(
                """\
            gitfs_saltenv_whitelist: base
            """
            )
        )
        with patch.dict(gitfs.__opts__, opts):
            gitfs.update()
            ret = gitfs.envs(ignore_cache=True)
            assert "base" in ret
            assert UNICODE_ENVNAME not in ret
            assert "mytag" not in ret

    @pytest.mark.slow_test
    def test_env_deprecated_opts(self):
        """
        ensure deprecated options gitfs_env_whitelist
        and gitfs_env_blacklist do not cause gitfs to
        not load.
        """
        opts = salt.utils.yaml.safe_load(
            textwrap.dedent(
                """\
            gitfs_env_whitelist: base
            gitfs_env_blacklist: ''
            """
            )
        )
        with patch.dict(gitfs.__opts__, opts):
            gitfs.update()
            ret = gitfs.envs(ignore_cache=True)
            assert "base" in ret
            assert UNICODE_ENVNAME in ret
            assert "mytag" in ret

    @pytest.mark.slow_test
    def test_disable_saltenv_mapping_global_with_mapping_defined_per_remote(self):
        """
        Test the global gitfs_disable_saltenv_mapping config option, combined
        with the per-saltenv mapping being defined in the remote itself via the
        "saltenv" per-remote option.
        """
        opts = salt.utils.yaml.safe_load(
            textwrap.dedent(
                """\
            gitfs_disable_saltenv_mapping: True
            gitfs_remotes:
              - {}:
                - saltenv:
                  - bar:
                    - ref: somebranch
            """.format(
                    self.tmp_repo_dir
                )
            )
        )
        with patch.dict(gitfs.__opts__, opts):
            gitfs.update()
            ret = gitfs.envs(ignore_cache=True)
            # Since we are restricting to tags only, the tag should appear in
            # the envs list, but the branches should not.
            self.assertEqual(ret, ["bar", "base"])

    @pytest.mark.slow_test
    def test_disable_saltenv_mapping_per_remote_with_mapping_defined_globally(self):
        """
        Test the per-remote disable_saltenv_mapping config option, combined
        with the per-saltenv mapping being defined in the global gitfs_saltenv
        option.
        """
        opts = salt.utils.yaml.safe_load(
            textwrap.dedent(
                """\
            gitfs_remotes:
              - {}:
                - disable_saltenv_mapping: True

            gitfs_saltenv:
              - hello:
                - ref: somebranch
            """.format(
                    self.tmp_repo_dir
                )
            )
        )
        with patch.dict(gitfs.__opts__, opts):
            gitfs.update()
            ret = gitfs.envs(ignore_cache=True)
            # Since we are restricting to tags only, the tag should appear in
            # the envs list, but the branches should not.
            self.assertEqual(ret, ["base", "hello"])

    @pytest.mark.slow_test
    def test_disable_saltenv_mapping_per_remote_with_mapping_defined_per_remote(self):
        """
        Test the per-remote disable_saltenv_mapping config option, combined
        with the per-saltenv mapping being defined in the remote itself via the
        "saltenv" per-remote option.
        """
        opts = salt.utils.yaml.safe_load(
            textwrap.dedent(
                """\
            gitfs_remotes:
              - {}:
                - disable_saltenv_mapping: True
                - saltenv:
                  - world:
                    - ref: somebranch
            """.format(
                    self.tmp_repo_dir
                )
            )
        )
        with patch.dict(gitfs.__opts__, opts):
            gitfs.update()
            ret = gitfs.envs(ignore_cache=True)
            # Since we are restricting to tags only, the tag should appear in
            # the envs list, but the branches should not.
            self.assertEqual(ret, ["base", "world"])


class GitFSTestBase:
    @classmethod
    def setUpClass(cls):
        cls.tmp_repo_dir = os.path.join(RUNTIME_VARS.TMP, "gitfs_root")
        if salt.utils.platform.is_windows():
            cls.tmp_repo_dir = cls.tmp_repo_dir.replace("\\", "/")
        cls.tmp_cachedir = tempfile.mkdtemp(dir=RUNTIME_VARS.TMP)
        cls.tmp_sock_dir = tempfile.mkdtemp(dir=RUNTIME_VARS.TMP)

        try:
            shutil.rmtree(cls.tmp_repo_dir)
        except OSError as exc:
            if exc.errno == errno.EACCES:
                log.error("Access error removing file %s", cls.tmp_repo_dir)
            elif exc.errno != errno.ENOENT:
                raise

        shutil.copytree(
            str(RUNTIME_VARS.BASE_FILES),
            str(cls.tmp_repo_dir + "/"),
            symlinks=True,
        )

        repo = git.Repo.init(cls.tmp_repo_dir)

        try:
            if salt.utils.platform.is_windows():
                username = salt.utils.win_functions.get_current_user()
            else:
                username = pwd.getpwuid(os.geteuid()).pw_name
        except AttributeError:
            log.error("Unable to get effective username, falling back to 'root'.")
            username = "root"

        with patched_environ(USERNAME=username):
            repo.index.add([x for x in os.listdir(cls.tmp_repo_dir) if x != ".git"])
            repo.index.commit("Test")

            # Add another branch with unicode characters in the name
            repo.create_head(UNICODE_ENVNAME, "HEAD")

            # Add a tag
            repo.create_tag(TAG_NAME, "HEAD")
            # Older GitPython versions do not have a close method.
            if hasattr(repo, "close"):
                repo.close()

    @classmethod
    def tearDownClass(cls):
        """
        Remove the temporary git repository and gitfs cache directory to ensure
        a clean environment for the other test class(es).
        """
        for path in (cls.tmp_cachedir, cls.tmp_sock_dir, cls.tmp_repo_dir):
            try:
                salt.utils.files.rm_rf(path)
            except OSError as exc:
                if exc.errno == errno.EACCES:
                    log.error("Access error removeing file %s", path)
                elif exc.errno != errno.EEXIST:
                    raise

    def setUp(self):
        """
        We don't want to check in another .git dir into GH because that just
        gets messy. Instead, we'll create a temporary repo on the fly for the
        tests to examine.

        Also ensure we A) don't re-use the singleton, and B) that the cachedirs
        are cleared. This keeps these performance enhancements from affecting
        the results of subsequent tests.
        """
        if not gitfs.__virtual__():
            self.skipTest("GitFS could not be loaded. Skipping GitFS tests!")

        _clear_instance_map()
        for subdir in ("gitfs", "file_lists"):
            try:
                salt.utils.files.rm_rf(os.path.join(self.tmp_cachedir, subdir))
            except OSError as exc:
                if exc.errno == errno.EACCES:
                    log.warning(
                        "Access error removeing file %s",
                        os.path.join(self.tmp_cachedir, subdir),
                    )
                    continue
                if exc.errno != errno.ENOENT:
                    raise
        if salt.utils.platform.is_windows():
            self.setUpClass()
            self.setup_loader_modules()


@skipIf(not HAS_GITPYTHON, "GitPython >= {} required".format(GITPYTHON_MINVER))
class GitPythonTest(GitFSTestBase, GitFSTestFuncs, TestCase, LoaderModuleMockMixin):
    def setup_loader_modules(self):
        opts = {
            "sock_dir": self.tmp_sock_dir,
            "gitfs_remotes": ["file://" + self.tmp_repo_dir],
            "gitfs_root": "",
            "fileserver_backend": ["gitfs"],
            "gitfs_base": "master",
            "gitfs_fallback": "",
            "fileserver_events": True,
            "transport": "zeromq",
            "gitfs_mountpoint": "",
            "gitfs_saltenv": [],
            "gitfs_saltenv_whitelist": [],
            "gitfs_saltenv_blacklist": [],
            "gitfs_user": "",
            "gitfs_password": "",
            "gitfs_insecure_auth": False,
            "gitfs_privkey": "",
            "gitfs_pubkey": "",
            "gitfs_passphrase": "",
            "gitfs_refspecs": [
                "+refs/heads/*:refs/remotes/origin/*",
                "+refs/tags/*:refs/tags/*",
            ],
            "gitfs_ssl_verify": True,
            "gitfs_disable_saltenv_mapping": False,
            "gitfs_ref_types": ["branch", "tag", "sha"],
            "gitfs_update_interval": 60,
            "__role": "master",
        }
        opts["cachedir"] = self.tmp_cachedir
        opts["sock_dir"] = self.tmp_sock_dir
        opts["gitfs_provider"] = "gitpython"
        return {gitfs: {"__opts__": opts}}


@skipIf(
    not HAS_GITPYTHON,
    "GitPython >= {} required for temp repo setup".format(GITPYTHON_MINVER),
)
@skipIf(
    not HAS_PYGIT2,
    "pygit2 >= {} and libgit2 >= {} required".format(PYGIT2_MINVER, LIBGIT2_MINVER),
)
@skipIf(
    salt.utils.platform.is_windows(),
    "Skip Pygit2 on windows, due to pygit2 access error on windows",
)
class Pygit2Test(GitFSTestBase, GitFSTestFuncs, TestCase, LoaderModuleMockMixin):
    def setup_loader_modules(self):
        opts = {
            "sock_dir": self.tmp_sock_dir,
            "gitfs_remotes": ["file://" + self.tmp_repo_dir],
            "gitfs_root": "",
            "fileserver_backend": ["gitfs"],
            "gitfs_base": "master",
            "gitfs_fallback": "",
            "fileserver_events": True,
            "transport": "zeromq",
            "gitfs_mountpoint": "",
            "gitfs_saltenv": [],
            "gitfs_saltenv_whitelist": [],
            "gitfs_saltenv_blacklist": [],
            "gitfs_user": "",
            "gitfs_password": "",
            "gitfs_insecure_auth": False,
            "gitfs_privkey": "",
            "gitfs_pubkey": "",
            "gitfs_passphrase": "",
            "gitfs_refspecs": [
                "+refs/heads/*:refs/remotes/origin/*",
                "+refs/tags/*:refs/tags/*",
            ],
            "gitfs_ssl_verify": True,
            "gitfs_disable_saltenv_mapping": False,
            "gitfs_ref_types": ["branch", "tag", "sha"],
            "gitfs_update_interval": 60,
            "__role": "master",
        }
        opts["cachedir"] = self.tmp_cachedir
        opts["sock_dir"] = self.tmp_sock_dir
        opts["gitfs_provider"] = "pygit2"
        return {gitfs: {"__opts__": opts}}
