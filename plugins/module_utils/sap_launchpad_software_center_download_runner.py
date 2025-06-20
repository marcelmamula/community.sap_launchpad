import hashlib
import json
import logging
import os
import time

from requests.auth import HTTPBasicAuth
from requests.exceptions import HTTPError

from . import constants as C
from .sap_api_common import _request, https_session
from .sap_id_sso import _get_sso_endpoint_meta
from .sap_launchpad_software_center_download_search_fuzzy import *

logger = logging.getLogger(__name__)

_HAS_DOWNLOAD_AUTHORIZATION = None
MAX_RETRY_TIMES = 3


def search_software_filename(name, deduplicate, search_alternatives):
    """
    Execute search for SAP Software or its alternative when search_alternatives is true.
    
    Args:
        name: The filename name to check (e.g. 'SAPCAR_1115-70006178.EXE').
        deduplicate: Select deduplication logic from 'first', 'last'
        search_alternatives: Boolean for enabling fuzzy search.

    Returns:
        download_link: Download link of matched SAP Software.
        filename: File name of matched SAP Software.
        alternative_found: True if alternative search was successful.
    """

    alternative_found = False
    software_search = _search_software(name)
    software_filtered = [r for r in software_search if r['Title'] == name or r['Description'] == name]

    files_count=len(software_filtered)
    if files_count == 0:
        # Run fuzzy search if search_alternatives was selected
        if search_alternatives:
            software_fuzzy_found = search_software_fuzzy(name)
            software_fuzzy_filtered, suggested_filename = filter_fuzzy_search(software_fuzzy_found, name)
            if len(software_fuzzy_filtered) == 0:
                raise ValueError(f'File {name} is not available to download and has no alternatives')

            software_fuzzy_alternatives = software_fuzzy_filtered[0].get('Title')          

            # Search has to be filtered again, because API call can get
            # duplicates like 70SWPM10SP43_2-20009701.sar for SWPM10SP43_2-20009701.SAR
            software_search_alternatives = _search_software(software_fuzzy_alternatives)
            software_search_alternatives_filtered = [
                file for file in software_search_alternatives
                if file.get('Title', '').startswith(suggested_filename)
            ]
            alternatives_count=len(software_search_alternatives_filtered)
            if alternatives_count == 0:
                raise ValueError(f'File {name} is not available to download and has no alternatives')
            elif alternatives_count > 1 and deduplicate == '':
                    names = [s['Title'] for s in software_search_alternatives_filtered]
                    raise ValueError('More than one results were found: %s. '
                                    'please use the correct full filename' % names)
            elif alternatives_count > 1 and deduplicate == 'first':
                    software_found = software_search_alternatives_filtered[0]
                    alternative_found = True
            elif alternatives_count > 1 and deduplicate == 'last':
                    software_found = software_search_alternatives_filtered[alternatives_count-1]
                    alternative_found = True
            else:
                    software_found = software_search_alternatives_filtered[0]
                    alternative_found = True
        else:
            raise ValueError(f'File {name} is not available to download. Enable "search_alternatives" to search for alternatives.')

    elif files_count > 1 and deduplicate == '':
            names = [s['Title'] for s in software_filtered]
            raise ValueError('More than one results were found: %s. '
                             'please use the correct full filename' % names)
    elif files_count > 1 and deduplicate == 'first':
            software_found = software_filtered[0]
    elif files_count > 1 and deduplicate == 'last':
            software_found = software_filtered[files_count-1]
    else:
            software_found = software_filtered[0]
    
    download_link = software_found['DownloadDirectLink']
    filename = _get_valid_filename(software_found)

    return (download_link, filename, alternative_found)


def download_software(download_link, filename, output_dir, retry=0):
    """Download software from DownloadDirectLink and save it as filename
    """
    # User might not have authorization to download software.
    if not _has_download_authorization():
        raise UserWarning(
            'You do not have proper authorization to download software, '
            'please check: '
            'https://launchpad.support.sap.com/#/user/authorizations')

    endpoint = download_link
    meta = {}

    # if SESSIONID is in the cookie list and it's valid,
    # then we can download file without SAML authentication
    # during tokengen (/tokengen/?file=fileid)
    if not https_session.cookies.get('SESSIONID',
                                     domain='.softwaredownloads.sap.com'):
        try:
            while ('SAMLResponse' not in meta):
                endpoint, meta = _get_sso_endpoint_meta(endpoint, data=meta)
            # 403 Error could be raised during the final SAML submit for tokengen.
            # If the request succeeds, it will be redirected to the real download URL.
            res = _request(endpoint, data=meta, stream=True)
        except HTTPError as e:
            # clear cookies including SESSIONID because we are not authed
            https_session.cookies.clear('.softwaredownloads.sap.com')
            if e.response.status_code != 403 or retry >= MAX_RETRY_TIMES:
                raise
            logger.warning('[403] Retry %d time(s) for %s',
                           retry+1, e.request.url)
            time.sleep(60*(retry+1))
            return download_software(download_link, filename, output_dir, retry+1)
        except ConnectionError as e:
            # builtin Connection Error is not handled by requests.
            if retry >= MAX_RETRY_TIMES:
                raise
            logger.warning('[ConnectionError] Retry %d time(s): %s', retry+1, e)
            time.sleep(60*(retry+1))
            return download_software(download_link, filename, output_dir, retry+1)

        res.close()
        endpoint = res.url

    logger.debug("real download url: %s", endpoint)
    filepath = os.path.join(output_dir, filename)
    _download_file(endpoint, filepath)


def is_download_link_available(url, retry=0):
    """Verify the DownloadDirectLink
    """
    # User might not have authorization to download software.
    if not _has_download_authorization():
        raise UserWarning(
            'You do not have proper authorization to download software, '
            'please check: '
            'https://launchpad.support.sap.com/#/user/authorizations')

    try:
        # if SESSIONID is in the cookie list and it's valid,
        # then we can download file without SAML authentication
        if not https_session.cookies.get('SESSIONID',
                                         domain='.softwaredownloads.sap.com'):
            meta = {}
            while ('SAMLResponse' not in meta):
                url, meta = _get_sso_endpoint_meta(url, data=meta)
            res = _request(url, stream=True, data=meta)
        else:
            res = _request(url, stream=True)
    except HTTPError as e:
        # clear cookies including SESSIONID because we are not authed
        https_session.cookies.clear('.softwaredownloads.sap.com')
        if e.response.status_code == 404:
            return False
        if e.response.status_code != 403 or retry >= MAX_RETRY_TIMES:
            raise
        logger.warning('[403] Retry %d time(s) for %s',
                       retry+1, e.request.url)
        time.sleep(60*(retry+1))
        return is_download_link_available(url, retry+1)
    except ConnectionError as e:
        # builtin Connection Error is not handled by requests.
        if retry >= MAX_RETRY_TIMES:
            raise
        logger.warning('[ConnectionError] Retry %d time(s): %s', retry+1, e)
        time.sleep(60*(retry+1))
        return is_download_link_available(url, retry+1)
    finally:
        _clear_download_key_cookie()

    # close explicitly is required for stream request.
    res.close()

    # test if we have a file download request in the end.
    content_header = res.headers.get('Content-Disposition')
    available = content_header and 'attachment;' in content_header
    return available


def download_software_via_legacy_api(username, password, download_link,
                                     filename, output_dir):
    filepath = os.path.join(output_dir, filename)

    _download_file(download_link,
                   filepath,
                   retry=0,
                   auth=HTTPBasicAuth(username, password))


def _search_software(keyword):
    
    url = C.URL_SOFTWARE_CENTER_SERVICE + '/SearchResultSet'
    params = {
        'SEARCH_MAX_RESULT': 500,
        'RESULT_PER_PAGE': 500,
        'SEARCH_STRING': keyword,
    }
    query_string = '&'.join([f'{k}={v}' for k, v in params.items()])
    query_url = '?'.join((url, query_string))

    headers = {'User-Agent': C.USER_AGENT_CHROME, 'Accept': 'application/json'}
    results = []
    try:
        res = _request(query_url, headers=headers, allow_redirects=False)
        j = json.loads(res.text)
        results = j['d']['results']
    except json.JSONDecodeError:
        # When use has no authority to search some specified files,
        # it will return non-json response, which is actually expected.
        # So just return an empty list.
        logger.warning('Non-JSON response returned for software searching')
        logger.debug(res.text)

    return results


def _download_file(url, filepath, retry=0, **kwargs):
    # Read response as stream, in case the file is huge.
    kwargs.update({'stream': True})
    try:
        res = _request(url, **kwargs)
        with open(filepath, 'wb') as f:
            # 1MiB Chunk
            for chunk in res.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
    except ConnectionError:
        # builtin Connection Error is not handled by requests.
        if retry >= MAX_RETRY_TIMES:
            # Remove partial file if exists.
            if os.path.exists(filepath):
                os.remove(filepath)
            raise
        time.sleep(60*(retry+1))
        return _download_file(url, filepath, retry+1, **kwargs)

    res.close()
    _clear_download_key_cookie()

    checksum = res.headers.get('ETag', '').replace('"', '')
    logger.debug("checksum: %s; url: %s", checksum, res.request.url)
    if (not checksum) or _is_checksum_matched(filepath, checksum):
        return
    logger.warning("checksum mismatch: %s: %s", filepath, checksum)
    if retry >= MAX_RETRY_TIMES:
        # Remove partial file if exists.
        if os.path.exists(filepath):
            os.remove(filepath)
        raise RuntimeError(f'failed to download {url}: md5 mismatch')
    return _download_file(url, filepath, retry+1, **kwargs)


def _has_download_authorization():
    global _HAS_DOWNLOAD_AUTHORIZATION
    if _HAS_DOWNLOAD_AUTHORIZATION is None:
        user_attributes = _request(C.URL_ACCOUNT_ATTRIBUTES).json()
        sid = user_attributes['uid']

        url = C.URL_SERVICE_USER_ADMIN + f"/UserSet('{sid}')/UserExistingAuthorizationsSet"
        j = _request(url, headers={'Accept': 'application/json'}).json()
        authorization_objs = [r['ObjectId'] for r in j['d']['results']]
        authorization_descs = [r['ObjectDesc'] for r in j['d']['results']]
        _HAS_DOWNLOAD_AUTHORIZATION = "Software Download" in authorization_descs or (True for x in ["SWDOWNLOAD", "G_SOFTDOWN"] if x in authorization_objs)
    return _HAS_DOWNLOAD_AUTHORIZATION


def _clear_download_key_cookie():
    # Software download server generates a cookie for every single file.
    # If we don't clear it after download, the cookie header will become
    # too long and the server will reject the request.
    for c in https_session.cookies:
        if c.domain == '.softwaredownloads.sap.com' and c.name != 'SESSIONID':
            https_session.cookies.clear(name=c.name, domain=c.domain, path='/')


def _is_checksum_matched(f, etag):
    # SAP Software Download Server is using MD5 and sha256 for ETag Header:
    # MD5    ETag: "e054445edd671fc1d01cc4f3dce6c84a:1634267161.876855"
    # SHA256 ETag: "14ce8940ff262ceb67823573b3dec3aee2b3cbb452c73601569d5876d02af8b0"
    checksum = etag.split(":")[0]
    hash = hashlib.md5()
    if len(checksum) == 64:
        hash = hashlib.sha256()
    with open(f, "rb") as f:
        for chunk in iter(lambda: f.read(4096 * hash.block_size), b""):
            hash.update(chunk)
    return hash.hexdigest() == checksum


def _get_valid_filename(software_found):
    """
    Ensure that CD Media have correct filenames from description.
    Example: S4CORE105_INST_EXPORT_1.zip downloads as 19118000000000004323

    Args:
        software_found: List[0] with dictionary of file.

    Returns:
        Valid filename for CD Media files, where applicable.
    """

    # Check if Title contains filename and extension
    if re.match(r'^\d+$', software_found['Title']):
        # Check if Description attribute exists and that it does not contain empty spaces
        if software_found['Description'] and ' ' not in software_found['Description']:
            return software_found['Description']
        else:
            return software_found['Title']
    else:
        # Default to Title if Description does not help
        return software_found['Title']
