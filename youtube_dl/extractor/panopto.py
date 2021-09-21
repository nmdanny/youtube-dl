# coding: utf-8
from __future__ import unicode_literals
from sys import stderr

from .common import InfoExtractor
from ..utils import ExtractorError, try_get, urljoin, urlencode_postdata, float_or_none
import re
import json


def handle_error_response(response, video_id):
    if response.get("ErrorCode") and "Unauthorized" in response.get("ErrorMessage"):
        raise ExtractorError("Need to login to access Panopto video, pass a cookie file as explained in https://github.com/ytdl-org/youtube-dl#how-do-i-pass-cookies-to-youtube-dl", expected=True)
    elif response.get("ErrorCode"):
        error_message = "Got error code %d: %s" % (response.get("ErrorCode"), response.get("ErrorMessage"))
        raise ExtractorError(error_message, video_id=video_id)


class PanoptoIE(InfoExtractor):
    _VALID_URL = r'(?P<panoptoBase>.*)/Panopto/Pages/Viewer.aspx\?id=(?P<id>[\w\-]+)'
    _TEST = {
        'url': 'https://demo.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=f97cb806-651b-4538-af8e-53700946eba1&start=0',
        'md5': '9fa69360c899f1e328786c355a8f3732',
        'info_dict': {
            'id': 'f97cb806-651b-4538-af8e-53700946eba1',
            'ext': 'mp4',
            'title': 'Use Case Videos -> Human Resources - Interview Best Practices',
            'description': 'In this video, Shawn Lipton, CEO of The Trusted Coach, provides hiring managers with his 5 top tips for conducting interviews.'
        }
    }

    def _try_parse_timestamps(self, delivery):
        vid_time = delivery.get("Duration")
        if not vid_time:
            return None
        timestamps = delivery.get("Timestamps")
        if not timestamps:
            return None
        chapters = []
        for cur_ts, next_ts in zip(timestamps, timestamps[1:] + [vid_time]):
            start = cur_ts.get("Time")
            end = next_ts if isinstance(next_ts, float) else next_ts.get("Time", vid_time)
            chapters.append({
                "start_time": float_or_none(start),
                "end_time": float_or_none(end),
                "title": cur_ts.get("Caption") or cur_ts.get("Data")
            })

        return chapters

    def _real_extract(self, url):
        match = re.match(self._VALID_URL, url)
        video_id = match.group('id')
        panopto_base = match.group("panoptoBase")
        return self._download_panopto_video(panopto_base, video_id)

    def _download_panopto_video(self, panopto_base, video_id):
        delivery_url = urljoin(panopto_base, 'Panopto/Pages/Viewer/DeliveryInfo.aspx')
        response = self._download_json(delivery_url, video_id, data=urlencode_postdata({
            "deliveryId": video_id,
            "responseType": "json"
        }))
        handle_error_response(response, video_id)
        delivery = response["Delivery"]

        title = " -> ".join([title_part for title_part in [
            delivery.get("SessionGroupLongName"),
            delivery.get("SessionName")
        ] if title_part]) or delivery.get("PublicID", video_id)

        formats = []
        for podcast in delivery.get("PodcastStreams", []):
            stream_url = podcast.get("StreamUrl")
            if stream_url:
                formats.append({
                    "url": stream_url,
                    "format_note": "podcast"
                })
        for stream in delivery.get("Streams", []):
            stream_url = stream.get("StreamUrl")
            if not stream_url:
                continue
            format = {
                "url": stream_url
            }
            stream_name = stream.get("Name") or ""
            stream_name_match = re.match(r"(?P<filename>.*).*\.(?P<ext>.*)", stream_name)
            if stream_name_match:
                stream_name = stream_name_match.group("filename")
                stream_ext = stream_name_match.group("ext")
                format["ext"] = stream_ext
                if "Shared screen" in stream_name:
                    format["format_note"] = "shared-screen"
                elif "Speaker view" in stream_name:
                    format["format_note"] = "speaker-view"
                else:
                    format["format_note"] = stream_name
            elif stream_name:
                format["format_note"] = stream_name
            else:
                format["format_note"] = "unknown"

            formats.append(format)

        if not formats:
            raise ExtractorError("Panopto video doesn't include any Podcast Streams")

        # a podcast will usually include both the speaker and
        #  the shared screen. Otherwise, prefer shared screen over speaker view.
        FORMAT_TO_ORDERING = {
            "podcast": 3,
            "shared-screen": 2,
            "speaker-view": 1,
        }
        formats = sorted(formats, key=lambda fmt: FORMAT_TO_ORDERING.get(fmt["format_note"], 0))

        return {
            'id': video_id,
            'title': title,
            'description': delivery.get("SessionAbstract"),
            'creator': delivery.get("OwnerDisplayName"),
            'duration': delivery.get("Duration"),
            'formats': formats,
            'chapters': self._try_parse_timestamps(delivery)
        }


class PanoptoFolderIE(InfoExtractor):
    _VALID_URL = r'(?P<panoptoBase>.*)/Panopto/Pages/Sessions/List.aspx#folderID=%22(?P<id>.+)%22'
    __PAGE_SIZE = 100

    def _real_extract(self, url):
        match = re.match(self._VALID_URL, url)
        panopto_base = match.group("panoptoBase")
        folder_id = match.group('id')
        get_folder_info_url = urljoin(panopto_base, 'Panopto/Services/Data.svc/GetFolderInfo')
        folder_response = self._download_json(get_folder_info_url, folder_id,
                                              data=json.dumps({
                                                  "folderID": folder_id
                                              }).encode('utf-8'), headers={
                                                  "Content-Type": 'application/json'
                                              })

        handle_error_response(folder_response, folder_id)
        folder_name = folder_response.get("Name")
        print("Gotten folder name: %s" % folder_name)

        get_sessions_url = urljoin(panopto_base, 'Panopto/Services/Data.svc/GetSessions')

        def fetch_session(page_num):
            sessions_response = self._download_json(get_sessions_url, folder_id, data=json.dumps({
                "queryParameters": {
                    "folderID": folder_id,
                    "page": page_num,
                    "maxResults": self.__PAGE_SIZE,
                    "includePlaylists": True,
                    "getFolderData": True,
                    "responseType": "json"
                }
            }).encode('utf-8'), headers={
                "Content-Type": 'application/json'
            })
            handle_error_response(sessions_response, folder_id)
            return sessions_response

        max_elements = None
        entries = []
        extractor = PanoptoIE(self._downloader)
        page_num = 0
        while max_elements is None or len(entries) < max_elements:
            session = fetch_session(page_num)
            max_elements = session["d"]["TotalNumber"]
            delivery_ids = [result["DeliveryID"] for result in session["d"]["Results"]]
            print("Fetching elements for page %d, number of videos: %d"
                  % (page_num, len(delivery_ids)))
            for delivery_id in delivery_ids:
                try:
                    video_entry = extractor._download_panopto_video(panopto_base, delivery_id)
                    entries.append(video_entry)
                except Exception as e:
                    print("Got error while fetching video ID %s: %s" % (delivery_id, e), file=stderr)
            page_num += 1
        
        return {
            "_type": "playlist",
            "title": folder_name,
            "id": folder_id,
            "entries": entries
        }
