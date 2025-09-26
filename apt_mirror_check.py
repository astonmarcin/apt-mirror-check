# coding: utf-8

import click
import os
import glob
import hashlib
import re
import sys
from pathlib import Path


class FileAttr(object):
    def __init__(self, path):
        self.path = path
        self.md5sum = ""
        self.sh256sum = ""
        self.sh512sum = ""
        self.size = 0


def parse_release_block_title_line(line):
    if line.startswith("MD5Sum:"):
        return True, "md5sum"
    elif line.startswith("SHA256:"):
        return True, "sh256sum"
    elif line.startswith("SHA512:"):
        return True, "sh512sum"
    else:
        return False, ""


def dist_attrs(dist_dir):
    """ Parse file attributes in Release file """
    attrs = {}

    for release in Path(dist_dir).rglob('Release'):
        with open(release.as_posix(), "rt") as f:
            in_block = False
            attr_name = ""

            for line in f.readlines():
                if in_block:
                    if not line.startswith(" "):
                        in_block, attr_name = parse_release_block_title_line(line)
                        continue
                    checksum, size, rname = line.split()
                    path = os.path.join(dist_dir, rname)

                    attr = attrs.get(path)
                    if attr is None:
                        attr = FileAttr(path)
                    attr.size = int(size)
                    setattr(attr, attr_name, checksum)
                    attrs[path] = attr

                elif not line.startswith(" "):
                    in_block, attr_name = parse_release_block_title_line(line)
                    continue

    return attrs


def pkg_attrs(pkg_desc_path):
    """ Opens Package file and loads it content as a attr """
    with open(pkg_desc_path, "rt") as f:
        attrs = {}
        last_key = None
        for line in f.readlines():
            line = line.rstrip('\n')
            if len(line.strip()) == 0:
                yield attrs
                attrs = {}
                last_key = None
            elif line.startswith(" "):  # last line continue
                if last_key is None:
                    raise ValueError
                attrs[last_key] += line
            else:
                sep_index = line.find(":")
                if sep_index < 0:
                    raise ValueError
                last_key = line[:sep_index]
                attrs[last_key] = line[sep_index + 2:]  # skip : and a space


def pool_attrs(dist_dir, pool_dir):
    """ Parse attributes in Packages file"""
    attrs = {}

    for root, _, files in os.walk(dist_dir):
        for filename in files:
            if filename != "Packages":
                continue
            for pkgattr in pkg_attrs(os.path.join(root, filename)):
                name = pkgattr["Filename"]
                if name.endswith(".deb"):
                    path = os.path.join(pool_dir, name)

                    attr = FileAttr(path)
                    attr.size = int(pkgattr.get("Size", "0"))
                    attr.md5sum = pkgattr.get("MD5sum", "")
                    attr.sh256sum = pkgattr.get("SHA256", "")

                    attrs[path] = attr
    return attrs


def is_checksum_correct(filepath, attr):
    s = os.stat(filepath)
    if attr.size != s.st_size:
        if filepath.endswith("Release"):
            return True
        print(filepath, "expected size: {}, but {}".format(attr.size, s.st_size))
        return False

    if len(attr.md5sum) != 0:
        m = hashlib.md5()
        expected_checksum = attr.md5sum
    elif len(attr.sh256sum) != 0:
        m = hashlib.sha256()
        expected_checksum = attr.sh256sum
    elif len(attr.sh512sum) != 0:
        m = hashlib.sha512()
        expected_checksum = attr.sh512sum
    else:
        return True

    with open(filepath, "rb") as f:
        while True:
            data = f.read(1024 * 1024)
            if not data:
                break

            m.update(data)
        checksum = m.hexdigest()

    if checksum != expected_checksum:
        print(filepath, "expected checksum: {}, but {}".format(expected_checksum, checksum))
        return False

    return True


def bad_files_in_dir(dirpath, attrs):
    for root, _, files in os.walk(dirpath):
        for filename in files:
            filepath = os.path.join(root, filename)
            if filepath in attrs:
                if not is_checksum_correct(filepath, attrs[filepath]):
                    yield filepath


def compare_in_release(release_path):
    inrelease_path = release_path.replace("Release", "InRelease")
    try:
        with open(release_path) as f:
            release_lines = f.read().splitlines()
        with open(inrelease_path) as f:
            inrelease_lines = f.read().splitlines()
    except FileNotFoundError:
        return
    start_idx, stop_idx = 0, 0
    for i, line in enumerate(inrelease_lines):
        if line.strip() == "-----BEGIN PGP SIGNED MESSAGE-----":
            start_idx = i + 3
        if line.strip() == "-----BEGIN PGP SIGNATURE-----":
            stop_idx = i
    if release_lines != inrelease_lines[start_idx:stop_idx]:
        print(inrelease_path, "differs from the Release file")
        yield inrelease_path


def trim_path(path, trim):
    path_splitted = path.split(os.sep)
    trimmers = [trim] if isinstance(trim, str) else trim
    for trimmer in trimmers:
        try:
            idx = path_splitted.index(trimmer)
            return os.sep.join(path_splitted[:idx])
        except ValueError:
            continue
    return path


def bad_files_in_mirror(mirror_dir, is_flat_repo):
    if is_flat_repo:
        pool_dir = os.path.normpath(mirror_dir)
        dist_dirs = [ pool_dir ]
    else:
        dist_root = os.path.join(mirror_dir, "dists")
        walker = os.walk(dist_root)
        _, subdirs, _ = next(walker)

        dist_dirs = [os.path.join(dist_root, subdir) for subdir in subdirs]
        #pool_dir = os.path.join(mirror_dir, "pool")
        pool_dir = trim_path(mirror_dir, "dists")

    for dist_dir in dist_dirs:
        release_path = next(glob.iglob(dist_dir+'/**/Release', recursive=True))
        click.echo("checking %s ..." % dist_dir)
        yield from compare_in_release(release_path)
        yield from bad_files_in_dir(dist_dir, dist_attrs(dist_dir))
        #yield from bad_files_in_dir(pool_dir, pool_attrs(dist_dir, pool_dir))
        for filename, attr in pool_attrs(dist_dir, pool_dir).items():
            if not is_checksum_correct(filename, attr):
                yield filename
    print("")


def all_mirrors(sites_dir):
    for site in next(os.walk(sites_dir))[1]:
        site_dir = os.path.join(sites_dir, site)
        is_flat_repo = True
        # Debian Repository Format - looking for dists directory
        for dists_dir in glob.iglob(site_dir+'/**/dists/', recursive=True):
            is_flat_repo = False
            if os.path.isdir(dists_dir):
                yield os.path.dirname(os.path.normpath(dists_dir)), False
        # Flat Repository Format, see https://wiki.debian.org/DebianRepository/Format#Flat_Repository_Format
        if is_flat_repo:
            for flat_dir in glob.iglob(site_dir+'/**/Release', recursive=True):
                if os.path.isfile(flat_dir):
                    yield os.path.dirname(os.path.normpath(flat_dir)), True


def find_base_path_in_config():
    try:
        with open("/etc/apt/mirror.list", "rt") as f:
            for line in f.readlines():
                m = re.match(r"set\s+base_path\s+([\w/-]+)", line)
                if m is None:
                    continue
                return m.group(1)
    except FileNotFoundError:
        pass


def get_sites_dir(base_dir):
    if base_dir is None:
        base_dir = find_base_path_in_config()
        if base_dir is None:
            base_dir = os.getcwd()

    sites_dir = os.path.join(base_dir, "mirror")  # NOTE: fixed as mirror
    if not os.path.isdir(sites_dir):
        raise click.BadOptionUsage("--base-dir", "please specify correct base_path the same as /etc/apt/mirror.list")

    return sites_dir


@click.command("Checking for corrupted files in apt-mirror files")
@click.option("-b", "--base-dir", type=click.Path(exists=True, file_okay=False, readable=True, resolve_path=True),
              help="apt-mirror base_path")
@click.option("--delete/--no-delete", is_flag=True, default=False, help="delete corrupted files")
def cli(base_dir, delete):
    sites_dir = get_sites_dir(base_dir)

    has_bad = False
    for mirror, is_flat_repo in all_mirrors(sites_dir):
        for bad_file in bad_files_in_mirror(mirror, is_flat_repo):
            has_bad = True

            if delete:
                os.unlink(bad_file)
                prefix = "[DELETED] "
            else:
                prefix = "[ERROR] "
            click.secho(prefix + bad_file, color="red")

    if not has_bad:
        click.echo("No error found!")
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    cli()
