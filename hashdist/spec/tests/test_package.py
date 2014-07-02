from pprint import pprint
from os.path import join as pjoin
from textwrap import dedent
from ...core.test.utils import *
from ...core.test.test_build_store import fixture as build_store_fixture
from .. import profile
from .. import package
from .. import package_loader
from .. import hook_api
from ..profile import PackageYAML
from ...formats.marked_yaml import marked_yaml_load, yaml_dump
from ..exceptions import ProfileError, PackageError
from hashdist.hdist_logging import null_logger
from nose import SkipTest


def test_topological_stage_sort():
    stages = [dict(name='z', value='z'),
              dict(name='a', value='a', before=['c', 'b']),
              dict(name='c', value='c'),
              dict(name='b', value='b'),
              dict(name='aa', value='aa', before='c', after='b')]
    stages = package_loader.normalize_stages(stages)
    stages = package_loader.topological_stage_sort(stages)
    assert stages == [{'value': 'a'}, {'value': 'b'}, {'value': 'aa'}, {'value': 'c'}, {'value': 'z'}]

#
# Build script assembly
#
def test_assemble_stages():
    doc = marked_yaml_load("""\
        build_stages:
        - {handler: bash, bash: './configure --with-foo={{foo}}'}
        - {handler: bash, bash: make}
        - {handler: bash, bash: make install}
    """)
    ctx = hook_api.PackageBuildContext(None, {}, {})
    ctx.parameters['foo'] = 'somevalue'
    p = package.PackageSpec("mypackage", doc, [], {})
    script = p.assemble_build_script(ctx)
    assert script == dedent("""\
    set -e
    export HDIST_IN_BUILD=yes
    ./configure --with-foo=somevalue
    make
    make install
    """)

def test_create_build_spec():
    package_spec = marked_yaml_load(dedent("""\
        name: mylib
        sources:
          - git: git://foo
            key: git:a3c39a03e7b8e9a3321d69ff877338f99ebb4aa2
        dependencies:
          build: [otherlib]
          run: [foolib] # should be ignored
        build_stages:
          - {name: make, handler: bash, bash: make}
        when_build_dependency:  # ignored below, only used in build spec of dependency
          - prepend_path: FOO
            value: value-of-FOO
    """))
    parameters = {"X": "x", "BASH": "/bin/bash"}
    build_spec = package.create_build_spec("mylib", package_spec,
                                           parameters, {"otherlib": "otherlib/abcdefg"}, {})
    expected = {
        "name": "mylib",
        "build": {
            "import": [{"ref": "OTHERLIB", "id": "otherlib/abcdefg"}],
            "commands": [
                {"set": "BASH", "nohash_value": "/bin/bash"},
                {"cmd": ["$BASH", "_hashdist/build.sh"]},
                {'hit': ['build-postprocess', '--shebang=multiline', '--write-protect', '--remove-pkgconfig',
                         '--relative-rpath', '--relative-symlinks', '--check-relocatable',
                         "--check-ignore=.*\\.pyc\\$",
                         "--check-ignore=.*\\.pyo\\$"]}]},
        "sources": [
            {"key": "git:a3c39a03e7b8e9a3321d69ff877338f99ebb4aa2", "target": "."}
            ]}
    assert expected == build_spec.doc



class MockPackageYAML(PackageYAML):

    def __init__(self, filename, doc):
        self.doc = doc
        self.filename = '/path/to/' + filename
        self.in_directory = False


class MockProfile(object):

    def __init__(self, files):
        self.parameters = {}
        self.packages = {}
        self.files = dict((name, marked_yaml_load(body)) for name, body in files.items())

    def load_package_yaml(self, name, parameters):
        try:
            filename = '%s.yaml' % name
            return MockPackageYAML(filename, self.files[filename])
        except AttributeError:
            return None

    def find_package_file(self, name, filename):
        return filename if filename in self.files else None


def test_prevent_diamond():
    files = {
        'a.yaml': 'extends: [b, c]',
        'b.yaml': 'extends: [d]',
        'c.yaml': 'extends: [d]',
        'd.yaml': '{}'}
    with assert_raises(PackageError):
        package.PackageSpec.load(MockProfile(files), 'a')

def test_inheritance_collision():
    files = {
        'child.yaml': 'extends: [base1, base2]',
        'base1.yaml': 'build_stages: [{name: stage1}]',
        'base2.yaml': 'build_stages: [{name: stage1}]'}
    with assert_raises(PackageError):
        package.PackageSpec.load(MockProfile(files), 'child')


def test_load_and_inherit_package():
    files = {}
    files['mypackage.yaml'] = """\
        extends: [base1, base2]
        dependencies:
          build: [dep1]
          run: [dep1]
        build_stages:

        - name: stage1_override
          # mode defaults to override
          a: 1

        - name: stage3_override
          mode: override
          a: 1

        - name: stage_to_remove
          mode: remove

        - name: stage4_replace
          mode: replace
          a: 1

        - name: stage_2_inserted
          after: stage1_override
          before: stage3_override
          a: 1

        profile_links:
        - name: start
          link: '*/**/*'
        when_build_dependency:
        - name: start
          set: FOO
          value: foovalue
    """

    files['base1.yaml'] = """\
        extends: [grandparent]
        build_stages:
        - random: anonymous
          handler: foo
    """

    files['base2.yaml'] = """\
        dependencies:
          run: [dep_base2_1]
        profile_links:
        - name: end
          after: start
          link: '*/**/*'
        - link: foo  # anonymous stage
        build_stages:
        - {name: stage3_override, after: stage1_override, a: 2, b: 3}
        - {name: stage4_replace, after: stage3_override, a: 2, b: 3}
    """

    files['grandparent.yaml'] = """\
        dependencies:
          build: [dep_gp_1]
        build_stages:
        - {name: stage1_override, a: 2, b: 3}
        - {name: stage_to_remove, after: stage1_override, a: 2, b: 3}
    """

    files['grandparent.py'] = '{}'
    files['base1.py'] = '{}'
    files['mypackage.py'] = '{}'
    files['mypackage.py'] = '{}'

    prof = MockProfile(files)

    loader = package_loader.PackageLoader('mypackage', {},
                                          load_yaml=prof.load_package_yaml,
                                          find_file=prof.find_package_file)
    assert set(p.name for p in loader.all_parents) == set(['base1', 'base2', 'grandparent'])
    assert set(p.name for p in loader.direct_parents) == set(['base1', 'base2'])
    assert loader.get_hook_files() == ['grandparent.py', 'base1.py', 'mypackage.py']

    # the below relies on an unstable ordering as the lists are not sorted, but
    # the parent traversal happens in an (unspecified) stable order
    expected = marked_yaml_load("""\
        build_stages:
        - {handler: foo, name: __3gcv3kvebeam4h3qzok75lxxt5n3rzr7, random: anonymous}
        - {a: 1, b: 3, name: stage1_override}
        - {a: 1, name: stage4_replace}
        - {a: 1, after: stage1_override, b: 3, name: stage3_override}
        - {a: 1, after: stage1_override, before: stage3_override, name: stage_2_inserted}
        dependencies:
          build: [dep1, dep_gp_1]
          run: [dep1, dep_base2_1]
        profile_links:
        - {link: '*/**/*', name: start}
        - {link: foo, name: __ypp7s5jlosieet6v4iyhvbg52ymlcwgk}
        - {after: start, link: '*/**/*', name: end}
        when_build_dependency:
        - {name: start, set: FOO, value: foovalue}
    """)
    print yaml_dump(loader.doc)
    assert expected == loader.doc


def test_order_stages():
    loader = package_loader.PackageLoader.__new__(package_loader.PackageLoader)
    loader.doc = marked_yaml_load("""\
    build_stages:
    - {a: 1, after: stage1_override, before: stage3_override, name: stage_2_inserted}
    - {a: 1, name: stage4_replace}
    - {a: 1, b: 3, name: stage1_override}
    - {a: 1, after: stage1_override, b: 3, name: stage3_override}
    """)

    expected = marked_yaml_load("""\
    build_stages:
    - {a: 1, b: 3, handler: stage1_override}
    - {a: 1, handler: stage_2_inserted}
    - {a: 1, b: 3, handler: stage3_override}
    - {a: 1, handler: stage4_replace}
    profile_links: []
    when_build_dependency: []
    """)
    assert expected == loader.stages_topo_ordered()


def test_extend_list():

    def _extend_list(to_insert, lst):
        """Removes items from `lst` that can be found in `to_insert`, and then
        returns a list with `to_insert` at the front and `lst` at the end.
        """
        lst = [x for x in lst if x not in to_insert]
        return to_insert + lst

    yield eq_, ['a', 'b'], _extend_list(['a'], ['b'])
    yield eq_, ['a', 'b'], _extend_list(['a'], ['b', 'a'])
    yield eq_, [], _extend_list([], [])
    # check that input is not mutated
    lst = ['a', 'a', 'a', 'a']
    yield eq_, ['a'], _extend_list(['a'], lst)
    yield eq_, ['a', 'a', 'a', 'a'], lst

def test_name_anonymous_stages():
    loader = package_loader.PackageLoader.__new__(package_loader.PackageLoader)
    loader.name = 'theloader'
    stages_ok = [
        {'name': 'foo'},
        {'before': 'foo', 'one': 1},
        {'before': 'bar', 'after': 'foo', 'two': 2},
        {}
        ]
    stages_identical = [
        {'before': 'foo', 'one': 1},
        {'before': 'bar', 'after': 'foo', 'one': 1},
        ]
    loader.doc = dict(ok=stages_ok, bad=stages_identical)
    result = loader.get_stages_with_names('ok')
    assert result == [{'name': 'foo'},
                      {'before': 'foo', 'name': '__vohxh6yicfhfiz6qerugjle42wfmlu2p', 'one': 1},
                      {'after': 'foo',
                       'before': 'bar',
                       'name': '__tdurutje3wctzqnu24mzy5gqza5yxt6b',
                       'two': 2},
                      {'name': '__qvk3jbqy6b3mvv5opou3ae55met2pt6s'}]
    with assert_raises(PackageError):
        loader.get_stages_with_names('bad')

def test_when_dictionary():
    doc = marked_yaml_load("""\
    dictionary:
        when platform == 'linux':
            when host:
                one: 1
            two: 2
    """)
    r = package_loader.recursive_process_conditionals(doc, {'platform': 'linux',
                                           'host': True})
    assert {'dictionary': {'one': 1, 'two': 2}} == r
    r = package_loader.recursive_process_conditionals(doc, {'platform': 'linux',
                                           'host': False})
    assert {'dictionary': {'two': 2}} == r
    r = package_loader.recursive_process_conditionals(doc, {'platform': 'windows',
                                           'host': False})
    assert {'dictionary': {}} == r

def test_when_list():
    doc = marked_yaml_load("""\
    dictionary:
    - when platform == 'linux':
      - when host:
        - 1
      - 2
    - 3
    - {nested: dict} # dict of length 1
    - am-on: host
      when: host
    """)
    r = package_loader.recursive_process_conditionals(
        doc, {'platform': 'linux', 'host': True})
    assert {'dictionary': [1, 2, 3, {'nested': 'dict'}, {'am-on': 'host'}]} == r
    r = package_loader.recursive_process_conditionals(
        doc, {'platform': 'linux', 'host': False})
    assert {'dictionary': [2, 3, {'nested': 'dict'}]} == r
    r = package_loader.recursive_process_conditionals(
        doc, {'platform': 'windows', 'host': False})
    assert {'dictionary': [3, {'nested': 'dict'}]} == r


@temp_working_dir_fixture
def test_files_glob(d):
    dump('pkgs/bar/bar.yaml', """
        dependencies:
          run: [dep]
        profile_links:
        - name: link_with_glob
          link: '*/**/*'
        - name: link_without_glob
          link: bar
        build_stages:
        - name: build_without_glob
          files: [bar.c]
        - name: build_with_glob
          files: [glob_*]
    """)
    dump('pkgs/bar/bar.c', '/*****/')
    dump('pkgs/bar/glob_first', 'Frist!')
    dump('pkgs/bar/glob_second', 'Second')
    dump('profile.yaml', """
        package_dirs:
        - pkgs
    """)
    with profile.TemporarySourceCheckouts(None) as checkouts:
        doc = profile.load_and_inherit_profile(checkouts, "profile.yaml")
        prof = profile.Profile(null_logger, doc, checkouts)
        pkg = package.PackageSpec.load(prof, 'bar')
    assert pkg.doc == marked_yaml_load("""
        dependencies:
          build: []
          run: [dep]
        profile_links:
        - {link: '*/**/*'}
        - {link: bar}
        build_stages:
        - files: [glob_first, glob_second]
          handler: build_with_glob
        - files: [bar.c]
          handler: build_without_glob
        when_build_dependency: []
    """)
