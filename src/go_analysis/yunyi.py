import requests
from requests.exceptions import HTTPError
import pandas as pd
import json
import os
from pysgf import SGF, SGFNode
from datetime import date

def valueOf(o, path):
    if (type(o) is not dict): return o
    value = ""
    root = o
    for key in path.split('/'):
        if (key in root): 
            value = root[key]
        if type(value) is dict:
            root = value
        else:
            break
    return value


class YunYi:

    def __init__(self, ca_file=None, level=None):
        self.ROOT_URL = 'https://weiqi-v2.ynwqxh.com'
        #ca_file = 'ynwqxh-com-chain.pem'
        self.tenant_id = '183'
        self.token = None
        self.level_map = {
                        31:'6dan', 30:'5dan', 29:'4dan', 28:'3dan', 27:'2dan', 26:'1dan'
                        ,25:'1k', 24:'2k', 23:'3k', 22:'4k', 21:'5k'
                    }
        self.ca_file = ca_file or 'ynwqxh-com-chain.pem'
        self.level = level or 30
    
    
    def call_api(self, url, headers=None, json=None)->list:
        data = []
        call_url = f"{self.ROOT_URL}{url}"
        try:
            if json is not None:
                response = requests.post(call_url, json=json, headers=headers, verify=self.ca_file)
            else:
                response = requests.get(call_url, headers=headers, verify=self.ca_file)
            # If the response was successful, no Exception will be raised
            response.raise_for_status()
        except HTTPError as http_err:
            print(f'HTTP error occurred: {http_err}')  # Python 3.6
        except Exception as err:
            print(f'Other error occurred: {err}')  # Python 3.6
        else:
            resp_json = response.json()
            if resp_json['success']==False:
                print(f"{resp_json['code']} - {resp_json['msg']}")
            else:
                data = resp_json['data']
        return data


    def login(self)->str:
        loginData = {
            'identifier': 'YOUR_PHONE', # placeholder
            'verification':'Imperial',
            'endName':'gui',
            'channel':'PASSWORD'
        }
        data = self.call_api('/go/user/login', json=loginData)
        # 'data': response JSON (user info redacted)
        if len(data)>0:
            self.token = data['token']
            return data['token']
        else:
            return ""

    def get_active_players(self, event, round=None):
        url = f"/go/arrange/arrangevs/pagequery?eventId={event}&pageNo=1&pageSize=500&roundNum={round or ''}"
        game_data = self.call_api(url)
        players = []
        p_codes = ['senteChessPlayerVO', 'goteChessPlayerVO']
        id_codes = ['senteNo', 'goteNo']
        players = [{
                "id": g[id_codes[i]], 
                "name": g[p]['name'], 
                "age": g[p]['age'], 
                "gender": g[p]['gender'], 
                "grade": g[p]['danGrading'],
                "idcard": g[p]['idcard'],
                "grade": g[p]['danGrading']
            } for g in game_data for i, p in enumerate(p_codes) if g[p] is not None]
        return players

    def get_games(self, level=28, update=False):
        """
        level is dan level; 28 = 3dan; 27 = 2dan
        """
        if update==False and os.path.exists("all_games.json"):
            with open("all_games.json",'rb') as fp:
                game_data = json.load(fp)['data']
        else:
            game_data = self.call_api(f"/go/event/batchquery?statusList%5B0%5D=3&statusList%5B1%5D=5&pageNo=1&pageSize=1500&typeList=&name=")
        games = [{
                "id":d['eventId'], 
                "name":d["name"], 
                "level":d['level'],
                "levelName":d['levelName'],
                "startTime": pd.to_datetime(d['startTime']).date(),
                "endTime": pd.to_datetime(d['endTime']).date(),
                "status": int(d['status'] or 0)
            #} for d in game_data if d['startLevel']==level and d['level']==level]
            } for d in game_data 
                if int(d['matchType'] or 0)==1 and int(d['status'] or 0)>=2
                    and int(d['fee'] or 0)>=10000 and int(d['certType'] or 2)==2
                    and int(d['startLevel'] or -1)<=level and int(d['startLevel'] or -1)>=21]
        print([g["id"] for g in games])

        for  g in games:
            result_fpath = f"results/result_{g['id']}.json"
            if os.path.exists(result_fpath) == False or g['status']==2:
                data = self.call_api(f"/go/grade/batchquery?eventId={g['id']}&pageNo=1&pageSize=2000")
                with open(result_fpath, 'w+') as fp:
                    json.dump(data, fp)
        return games

    def get_game_results(self,eventId, players=None):
        result_fpath = f"results/result_{eventId}.json"
        data = []
        if os.path.exists(result_fpath):
            with open(result_fpath,'rb') as fp:
                data = json.load(fp)
        else:
            data = self.call_api(f"/go/grade/batchquery?eventId={eventId}&pageNo=1&pageSize=2000")
            #with open(result_fpath, 'w+') as fp:
            #    json.dump(data, fp)

        if players is None:
            all_results = [{
                    "game_id":r['eventId'], 
                    "startTime":pd.to_datetime(r['gmtCreate']).date(), 
                    "player_id":r['no'],
                    "player_name":r['chessPlayerVO']['name'],
                    "player_idcard":r['chessPlayerVO']['idcard'],
                    "player_gender":r['chessPlayerVO']['gender'],
                    "player_age":r['chessPlayerVO']['age'],
                    "player_rank":r['chessPlayerVO']['danGrading'],
                    "score":r['score'],
                    "totalScore":r['totalScore'],
                    "oppScore":r['oppScore'],
                    "smallScore":r['smallScore'],
                    "winOppScore":r['winOppScore'],
                    "negScore":r['negScore'],
                    "winRate":r['winRate'],
                    "winNum":r['winNum'],
                    #"middleOppScore":r['middleOppScore'],
                    #"highestOppScore":r['highestOppScore'],
                    "continuousWin":r["continuousWin"],
                    "oppToOppScore":r["oppToOppScore"],
                    "rank":r['rank']
                } for r in data]
            return all_results
        else:
            for p1 in players:
                x = [r for r in data if r['chessPlayerVO']['idcard'] == p1['idcard']]
                if len(x) > 0:
                    r = x[0]
                    p1.update({
                        "game_id":r['eventId'], 
                        "startTime":pd.to_datetime(r['gmtCreate']).date(), 
                        "player_id":r['no'],
                        "score":r['score'],
                        "totalScore":r['totalScore'],
                        "oppScore":r['oppScore'],
                        "smallScore":r['smallScore'],
                        "winOppScore":r['winOppScore'],
                        "negScore":r['negScore'],
                        "winRate":r['winRate'],
                        "continuousWin":r["continuousWin"],
                        "oppToOppScore":r["oppToOppScore"],
                        "rank":r['rank']
                    })

    def download_sgf(self, event, player_no=None):
        global ca_file, ROOT_URL, tenant_id, token
        if token is None: token = self.login()
        heads = {
            "token": token,
            "tenantId" : str(tenant_id)
        }
        players = []
        if type(player_no) is list:
            players.extend(player_no)
        else:
            players.append(player_no)
        
        games = []
        total_players = len(players)
        for i, player in enumerate(players):
            userData = self.call_api(f"/go/arrange/user/arrangevs/batchquery?eventId={event}&no={player}", headers=heads)
            if len(userData) == 0: break
            print(f'\rGet game details {i+1} of {total_players}: -> {len(userData)}...', end="\r")
            games.extend([{
                    "id": g['arrgVsId'],
                    "seq": g['arrgId'],
                    "roundNum": g['roundNum'],
                    "tableNum": g['tableNo'],
                    'result': valueOf(g, 'exnInf/resultDesc'),
                    'blackNo': g['senteNo'],
                    'whiteNo': g['goteNo'],
                    'blackName': valueOf(g, 'senteChessPlayerVO/name'),
                    'blackAge': valueOf(g, 'senteChessPlayerVO/age'),
                    'blackGender': valueOf(g, 'senteChessPlayerVO/gender'),
                    'blackGrade': valueOf(g, 'senteChessPlayerVO/danGradingName'),
                    'blackScore': valueOf(g, 'senteChessPlayerVO/score'),
                    'whiteName': valueOf(g, 'goteChessPlayerVO/name'),
                    'whiteAge': valueOf(g, 'goteChessPlayerVO/age'),
                    'whiteGender': valueOf(g, 'goteChessPlayerVO/gender'),
                    'whiteGrade': valueOf(g, 'goteChessPlayerVO/danGradingName'),
                    'whiteScore': valueOf(g, 'goteChessPlayerVO/score')
                } for g in userData if g['status']<=2]) # if g['status']==2 , "status==7 means in game"
        print('\n')
        sgf_path = os.path.join(f"sgf_{event}")
        if not os.path.exists(sgf_path):
            os.makedirs(sgf_path, exist_ok=True)
        try:
            total_games = len(games)
            for i, g in enumerate(games):
                if g['blackNo'] is None or g['whiteNo'] is None: continue
                fname = f"yunyi_{g['blackNo']}_{g['whiteNo']}_{g['id']}.sgf"
                fpath = os.path.join(sgf_path, fname)
                if not os.path.exists(fpath):
                    resp = requests.get(f"{ROOT_URL}/go/playchess/match/sgf/download.resource?arrgVsId={g['id']}&tenantId={tenant_id}&token={token}", verify=ca_file)
                    if len(resp.content)>100: 
                        game_tree = SGF.parse(resp.content.decode('utf-8'))
                        game_tree.root.set_property('KM',7.5)
                        game_tree.root.set_property('CA','UTF-8')
                        game_tree.root.set_property('RU','chinese')
                        game_tree.root.set_property('DT',f'{date.today()}')
                        with open(fpath, 'wb+') as fp:
                            fp.write(game_tree.sgf().encode('utf-8'))
                        print(f"\rDownload game: {fname:<15} ... {i+1:<4} of {total_games:6}  ", end="\r")
        except Exception as ex:
            print(ex)
        finally:
            pass
        return games