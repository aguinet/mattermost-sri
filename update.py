# For each plugin in list.plugins, download the releases we don't know, and get
# the hashes
import sys
import yaml
import aiohttp
import asyncio
import pprint
import collections
import tarfile
import hashlib
import functools
import base64
import json
import pathlib
from syncer import sync

import nest_asyncio
nest_asyncio.apply()

Release = collections.namedtuple('Release', ['plugin_id', 'tag', 'url'])

def compute_hash(fileobj):
    m = hashlib.sha384()
    for buf in iter(functools.partial(fileobj.read, 64*1024), b''):
        m.update(buf)
    digest = m.digest()
    # Returns "sha384-base64digest"
    return "sha384-%s" % base64.b64encode(digest).decode("ascii")

class GithubService:
    API_BASE = "https://api.github.com"
    def __init__(self, plugin_id, repository, session):
        self.repository = repository
        self.plugin_id = plugin_id
        self.session = session

    async def releases(self):
        uri = f"repos/{self.repository}/releases"
        releases = await self.doAPI(uri)
# [{'assets': [{'browser_download_url': 'https://github.com/quarkslab/mattermost-plugin-e2ee/releases/download/v0.6.1/com.quarkslab.e2ee-0.6.1.tar.gz',
#              'content_type': 'application/octet-stream',
#              'created_at': '2021-10-10T17:39:35Z',
#              'download_count': 8,
#              'id': 46655220,
#              'label': '',
#              'name': 'com.quarkslab.e2ee-0.6.1.tar.gz',
#              'node_id': 'RA_kwDOGIu8as4Cx-b0',
#              'size': 32670460,
#              'state': 'uploaded',
#              'updated_at': '2021-10-10T17:39:36Z',
#              'url': 'https://api.github.com/repos/quarkslab/mattermost-plugin-e2ee/releases/assets/46655220'}],
#  'assets_url': 'https://api.github.com/repos/quarkslab/mattermost-plugin-e2ee/releases/51103566/assets',
#  'id': 51103566,
#  'name': 'v0.6.1',
#  'node_id': 'RE_kwDOGIu8as4DC8dO',
#  'prerelease': False,
#  'tag_name': 'v0.6.1',
        out = []
        for r in releases:
            tag = r['tag_name']
            # Parse assets to find the package
            for a in r['assets']:
                name = a['name']
                if name.endswith(".tar.gz"):
                    url = a['browser_download_url']
                    break
            else:
                print(f"[-] unable to find download package for plugin '{self.plugin_id}' / tag '{tag}'", file=sys.stderr)
                continue
            out.append(Release(plugin_id=self.plugin_id,tag=r['tag_name'], url=url))
        return out

    async def doAPI(self, uri):
        headers = {
            "Accept": "application/vnd.github.v3+json"
        }
        url = f'{self.API_BASE}/{uri}'
        async with self.session.get(url, headers=headers) as resp:
            return await resp.json()

class AsyncStreamToSync:
    def __init__(self, stream):
        self.stream = stream

    def read(self, n):
        return sync(self.stream.read(n))

PluginInfo = collections.namedtuple('PluginInfo', ['plugin_id', 'tag', 'version', 'sri_hash'])

async def version_and_hash_from_package(session, url):
    print("[+] Processing %s ..." % url)
    version = None
    sri_hash = None
    async with session.get(url) as resp:
        tar = tarfile.open(fileobj=AsyncStreamToSync(resp.content), mode='r|gz')
        while True:
            if version is not None and sri_hash is not None:
                break
            member = tar.next()
            if member is None: break
            if not member.isfile(): continue

            name = member.name
            if name.endswith("/plugin.json"):
                # Extract the actual version number
                data = tar.extractfile(member)
                try:
                    version = json.load(data).get('version', None)
                except json.JSONDecodeError as e:
                    print(f"[-] unable to decode json in package '{url}'", file=sys.stderr)
                    return None
                if version is None:
                    print(f"[-] unknown version in manifest for package '{url}'", file=sys.stderr)
                    return None
                continue
            if name.endswith("/webapp/dist/main.js"):
                # Compute hash
                sri_hash = compute_hash(tar.extractfile(member))
                continue
        if version is None:
            print(f"[-] unable to get version for package '{url}'", file=sys.stderr)
            return None
        if sri_hash is None:
            print(f"[-] unable to get javascript file data for package '{url}'", file=sys.stderr)
            return None
        return (version, sri_hash)

async def get_plugin_info(session, plugin_id, release):
    version_sri_hash = await version_and_hash_from_package(session, release.url)
    if version_sri_hash is None:
        return PluginInfo(plugin_id=plugin_id, tag=release.tag, version=None, sri_hash=None)
    version,sri_hash = version_sri_hash
    return PluginInfo(plugin_id=plugin_id, tag=release.tag, version=version, sri_hash=sri_hash)

async def main():
    plugins = yaml.safe_load(open("plugins.yaml"))
    all_known_tags_ = json.load(open("plugins/known_tags.json"))
    all_known_tags = collections.defaultdict(set)
    all_known_tags.update((k, set(v)) for k,v in all_known_tags_.items())

    async with aiohttp.ClientSession() as session:
        co_releases = []
        for plugin_id, data in plugins.items():
            assert(data["service"] == "github")
            gh = GithubService(plugin_id, data["repository"], session)
            co_releases.append(gh.releases())

        pis = []
        for releases in asyncio.as_completed(co_releases):
            for r in await releases:
                known_tags = all_known_tags.get(r.plugin_id, set())
                if r.tag in known_tags:
                    print(f"[x] skipping known plugin '{r.plugin_id}' / tag '{r.tag}'", file=sys.stderr)
                    continue
                pis.append(get_plugin_info(session, r.plugin_id, r))

        for pi in asyncio.as_completed(pis):
            pi = await pi
            all_known_tags[pi.plugin_id].add(pi.tag)
            if pi.version is None:
                continue
            print(f"[+] writing information for '{pi.plugin_id}' version '{pi.version}'")
            dir_ = pathlib.Path('.') / "plugins" / pi.plugin_id
            dir_.mkdir(parents=True, exist_ok=True)
            fn = dir_ / pi.version
            open(fn,"w").write('{"sri_hash": "%s"}' % pi.sri_hash)

    all_known_tags = {k: list(v) for k,v in all_known_tags.items()}
    json.dump(all_known_tags, open("plugins/known_tags.json","w"))

asyncio.run(main())
