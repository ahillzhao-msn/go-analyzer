#import dill
import os, re, sys, time
import glob
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pickle
from IPython.display import clear_output
try:
    os.environ['KIVY_NO_ARGS'] = '1'
    os.environ["KCFG_KIVY_LOG_LEVEL"] = "warning"
    sys.path.append(os.path.dirname(__file__))
    from katrain.core.ai import game_report
    from katrain.core.base_katrain import KaTrainBase
    from katrain.core.engine import KataGoEngine
    from katrain.core.game import Game, KaTrainSGF
    from katrain.core.game_node import GameNode
    from katrain.core.sgf_parser import SGFNode, Move
except:
    clear_output(wait=True)
    print('Failed to load Katrain library!')
    pass
import sqlite3 as sql

class GoPredict:
    settings = {
        "fast_visits": 25,
        "max_visits": 640,
        #"threads": 256,
        "model": os.path.join(os.environ['USERPROFILE'], ".katrain\\kata1-b28c512nbt-s7168446720-d4316919285.bin.gz") # ".katrain\\kata1-b18c384nbt-s9761732864-d4253420187.bin.gz")
    }

    def __init__(self, model_path=None, db_path=None) -> None:
        pd.set_option("display.max_rows", 5000)

        self.MODEL_PATH = model_path or os.path.join(os.path.dirname(__file__), "modelTrained.pkl")
        self.DB_PATH = db_path or os.path.join(os.path.dirname(__file__), "go_games.db")

        # settings["model"] = "C:\\Users\\sande\\.katrain\\g170e-b20c256x2-s5303129600-d1228401921.bin.gz"
        # settings["model"] = "C:\\Users\\sande\\.katrain\\kata1-b40c256-s11840935168-d2898845681.bin.gz"

        #katrain = KaTrainBase(force_package_config=True, debug_level=0)
        katrain = KaTrainBase()
        combined_settings = {**katrain.config("engine"), **self.settings}
        self.engine = KataGoEngine(katrain, combined_settings)
        self.thresholds = katrain.config("trainer/eval_thresholds")
        self.reports = []
        self.cands_list = []
        self.filtered_cands_list = []
        for i in range(4):
            time.sleep(1.2)
            clear_output(wait=False)
            print("", flush=True)
        pass

    def sgf_deep_analysis(self, sgf_filename, bad_threshold=7.6, good_winrate=0.70):
        #null_fd = os.open(os.devnull, os.O_WRONLY) # Create a new file object
        #sys.stdout = os.fdopen(null_fd, 'w') # Replace stdout with the null file descriptor
        move_tree:GameNode = KaTrainSGF.parse_file(sgf_filename)
        self.engine.on_new_game()
        game = Game(katrain=self.engine.katrain,  engine=self.engine, move_tree=move_tree, analyze_fast=True, sgf_filename=sgf_filename)
        game.analyze_all_nodes(analyze_fast=True)
        #sys.stdout = sys.__stdout__ # Reset the standard output
        clear_output()
        while not move_tree.analysis_complete or not self.engine.is_idle():
            query_remain = self.engine.queries_remaining()
            print(f'\rAnalyzing the game...{query_remain} queries left <<<', end='\r')
            time.sleep(0.5)
        print('\rEnd of analysis\n', end='\r')
        highlights = []
        for node in move_tree.nodes_in_tree:
            if node.empty or node.is_root or node.is_pass: continue
            if (node.points_lost is None): continue
            bad = node.points_lost >= bad_threshold
            good = node.points_lost < -1 * bad_threshold
            if node.move.player == 'B':
                good = good and node.winrate >= good_winrate
            else:
                good = good and node.winrate < 1 - good_winrate

            if bad or good:
                stat = b'\xe6\x81\xb6\xe6\x89\x8b' if bad else b'\xe5\xa6\x99'
                highlights.extend([{
                    "move_no": node.depth,
                    "Stat": stat.decode(),
                    "player": node.player,
                    "pos": node.move.gtp(),
                    "coord": node.move.coords,
                    "score": node.format_score(),
                    "winrate": node.format_winrate(),
                    "points_loss": node.points_lost
                }])
        #report = game_report(game, self.thresholds)
        sum_stats, histogramJD, player_ptlossJD = game_report(game, thresholds=self.thresholds)
        del game, move_tree
        clear_output(wait=True)
        return highlights, sum_stats, histogramJD, player_ptlossJD

    def sgf_analysis(self, analysis_path="data", ignore_games=[], board_size=None):
        clear_output(wait=True)
        ANALYSIS_CAPACITY = 30000

        # Loading the saved game list, this avoids the duplicated analysis.
        saved_game_path = os.path.join(analysis_path, f"prediction.xlsx")
        df_saved = None
        if os.path.exists(saved_game_path):
            df_saved = pd.read_excel(saved_game_path, sheet_name="data")
            ignore_games.extend(df_saved['game_id'].drop_duplicates().to_list())
            pass

        # The list of games to be analyzed.
        games = []
        n = 0
        self.engine.on_new_game() # Reset Engine

        # Load the games into KaTrain, and then start the analysis of each games.
        for sgf in glob.iglob("*.sgf", root_dir=os.path.join(analysis_path)):
            game_id = glob.escape(sgf).removesuffix(".sgf")
            if game_id in ignore_games:
                continue
            with open(os.path.join(analysis_path, sgf), encoding="utf8") as f:
                move_tree = KaTrainSGF.parse_sgf(f.read())
            if board_size is not None:
                if move_tree.board_size[0] != board_size:
                    continue
            partie = Game(self.engine.katrain, self.engine, move_tree=move_tree, analyze_fast=True, sgf_filename=sgf)
            partie.game_id = game_id
            #move_tree.nodes_in_tree[-1].analyze(self.engine, analyze_fast=False)  # speed up result for looking at end of game
            partie.analyze_all_nodes(analyze_fast=True)
            games.append(partie)
            n += 1
            if n >= ANALYSIS_CAPACITY:  # small test=3
                break

        # Monitoring the analysis progress, it might takes time to complete.
        query_remaining = 999
        time.sleep(1)
        while not self.engine.is_idle() or query_remaining > 0:
            query_remaining = self.engine.queries_remaining() if not self.engine.is_idle() else 0
            print(f"\r>>> waiting for engine to finish...{query_remaining} queries left <<<", end="\r")
            time.sleep(0.5)
        self.engine.shutdown(finish=None)
        print(f"\n=== End of analysis ===", end="\n")

        # Extracting all stats of the games, then run the predictation for normalized ranking.
        print(f"\r>>> Running game reporting...", end="\r")
        df4, y_pred, y_pred_rank = self._game_analysis(games, size=board_size)

        # Save the results back to an Excel file called: <Prediction.xlsx>.
        if df4 is not None:
            print(f"\r>>> Saving game reports... >>>", end="\r")
            df5 = pd.DataFrame(df4['Pseudo'].tolist(), columns=['name', 'rank', 'op_rank', 'color', 'game_id', 'game_name','result','size','game_date'])
            df5['game_date'] = pd.to_datetime(df5['game_date'], errors='ignore', format='%Y-%m-%d')
            df5.insert(loc=9,column='Re',value=None)
            df5.insert(loc=9,column='Grade',value=None)

            df_result=pd.concat([df5.reset_index(drop=True)
                ,pd.Series(y_pred,name="RankPredict").reset_index(drop=True)
                ,pd.Series(y_pred_rank,name="RankNormalize").reset_index(drop=True)
                ,df4.drop('Pseudo',axis=1).reset_index(drop=True)],axis=1)
            df_result['Grade'] = df_result['RankNormalize'].apply(lambda x: float(x[:-1]) * (1 if x[-1]=='d' else -1))
            df_result['Re'] = df_result.apply(lambda x: 'Win' if x['color']==str(x['result'] or 'B')[0] else 'Loss', axis=1)

            if df_saved is not None:
                df_to_save = pd.concat([df_saved.reset_index(drop=True), df_result.reset_index(drop=True)]).reset_index(drop=True)
            else:
                df_to_save = df_result.reset_index(drop=True)
            #df_result.to_csv("data/prediction.csv")
            if os.path.exists(saved_game_path):
                #os.rename(saved_game_path, os.path.join(apath, f"prediction{uname}_old.xlsx"))
                with pd.ExcelWriter(saved_game_path.strip(), mode='a', if_sheet_exists='replace') as writer:
                    df_to_save.to_excel(writer, sheet_name='data', index=False)
            else:
                df_to_save.to_excel(saved_game_path, sheet_name='data', index=False)
            print(f"\n=== End of reporting === {saved_game_path}", end="\n")
            return games, pd.DataFrame(df_result[['game_id','name','color','Re','RankNormalize']])
        else:
            print(f"\n=== End of program ===", end="\n")

        return games, None
        # End of sgf_analysis


    def _game_analysis(self, games: list[Game], size=None):
        """
        Based on nomorlized model to predict the GO ranks.
        """
        df4 = None  # intialize the dataframe

        if size is not None:
            selected_games = [g for g in games if g.board_size[0] == size and len(g.root.children) > 0]
        else:
            selected_games = [g for g in games if len(g.root.children) > 0]
        for game in selected_games:
            try:
                test_props = ['BR','WR']
                for prop in test_props:
                    if not prop in game.root.properties:
                        game.root.set_property(prop, "")
                if game.root.get_property("BR")=='': game.root.set_property("BR", "9d")
                if game.root.get_property("WR")=='': game.root.set_property("WR", "9d")
                sum_stats, histogramJD, player_ptlossJD = game_report(game, thresholds=self.thresholds)

                for bw in "BW":
                    oppbw = "B" if bw == "W" else "W"
                    info = {
                        "name": game.root.get_property(f"P{bw}", "??"),
                        "rank": game.root.get_property(f"{bw}R", "9p"),
                        "opp_rank": game.root.get_property(f"{oppbw}R", "9p"),
                        "game_id": game.root.get_property("GN", game.game_id),
                        "result": game.root.get_property("RE", "??"),
                        "game_date": game.root.get_property("DT", ""),
                        "size": game.root.get_property("SZ", "19"),
                        **sum_stats[bw],
                    }
                    self.reports.append(info)

                df = pd.DataFrame(self.reports).sort_values(
                    by="accuracy", ascending=False).reset_index(drop=True)
                # df = pd.DataFrame(reports).reset_index(drop=True)
                # df.name = [unidecode(n) for n in df.name]
                # print(df)

                df["numrank"] = [dan(rank) for rank in df["rank"]]

                white = pd.Series(player_ptlossJD["W"])
                black = pd.Series(player_ptlossJD["B"])
                # white.plot()
                # black.plot()
                whiteTop1 = sum_stats["W"]["ai_top_move"] if len(sum_stats["W"]) > 0 else 0
                whiteTop5 = sum_stats["W"]["ai_top5_move"] if len(sum_stats["W"]) > 0 else 0
                whiteAccuracy = sum_stats["W"]["accuracy"] if len(sum_stats["W"]) > 0 else 0
                whiteComplexity = sum_stats["W"]["complexity"] if len(sum_stats["W"]) > 0 else 0
                blackTop1 = sum_stats["B"]["ai_top_move"] if len(sum_stats["B"]) > 0 else 0
                blackTop5 = sum_stats["B"]["ai_top5_move"] if len(sum_stats["B"]) > 0 else 0
                blackAccuracy = sum_stats["B"]["accuracy"] if len(sum_stats["B"]) > 0 else 0
                blackComplexity = sum_stats["B"]["complexity"] if len(sum_stats["B"]) > 0 else 0

                percentileBlack = np.percentile(black, np.arange(5, 101, 5)).tolist()
                percentileWhite = np.percentile(white, np.arange(5, 101, 5)).tolist()
                
                index = ["Pseudo", "RangJ", "RangAdv", "Moyenne", "Mediane", "Top1", "Top5", "Accuracy", "Complexity"]
                index.extend(["p"+str(x) for x in np.arange(5, 101, 5).tolist()])

                pseudo = [game.game_id, game.root.get_property("GN", game.root.get_property("EV", "??")), game.root.get_property("RE"), game.root.get_property("SZ"), game.root.get_property("DT")]
                pseudoB = [game.root.get_property("PB"), game.root.get_property("BR"), game.root.get_property("WR"), "B"]
                pseudoB.extend(pseudo)
                dataNoir = [pseudoB, rankingChiffre(game.root.properties["BR"]), rankingChiffre(game.root.properties["WR"]), black.mean(), black.median(), blackTop1, blackTop5, blackAccuracy, blackComplexity]
                dataNoir.extend(percentileBlack)
                pseudoW = [game.root.get_property("PW"), game.root.get_property("WR"), game.root.get_property("BR"), "W"]
                pseudoW.extend(pseudo)
                dataBlanc = [pseudoW, rankingChiffre(game.root.properties["WR"]), rankingChiffre(game.root.properties["BR"]), white.mean(), white.median(), whiteTop1, whiteTop5, whiteAccuracy, whiteComplexity]
                dataBlanc.extend(percentileWhite)

                noir = pd.Series(dataNoir, index=index)  # Black
                blanc = pd.Series(dataBlanc, index=index)  # White

                df3 = pd.concat([noir, blanc], axis=1).T
                if 'df4' in locals():
                    df4 = pd.concat([df4, df3])
                else:
                    df4 = df3

            except Exception as e:
                print(f"[{game.game_id}] Error raised: ", e)
                continue  # ignor this game, but continue

        # Getting results from ML model
        if df4 is not None:
            for i in df4.columns[3:]:
                df4[i] = df4[i].astype("float32")

            with open(self.MODEL_PATH, 'rb') as handle:
                model = pickle.load(handle)
            X = df4.copy()
            y_theorique = X.RangJ
            for i in ["Unnamed: 0", "RangJ", "Pseudo", "RangAdv"]:
                if i in X.columns:
                    X.drop(i, axis=1, inplace=True)

            y_pred = model.predict(X.values)
            y_pred_rank = np.round(y_pred, 1)
            y_pred_rank = [str(x)+"d" if x > 0 else str(abs(x)+1) + "k" for x in y_pred_rank]

            return df4, y_pred, y_pred_rank
        else:
            return None, None, None


def subplot(sp, ynames):
    global df
    plt.subplot(2, 2, sp)
    legend = []
    xfull = np.array(range(df["numrank"].min(), df["numrank"].max() + 1))
    cols = "bgr"
    for i, yname in enumerate(ynames):
        plt.plot(df["numrank"], df[yname], cols[i] + "x")
    for i, yname in enumerate(ynames):
        fit = polyfit(df["numrank"], df[yname])
        a, b = fit["coef"]
        plt.plot(xfull, xfull * a + b, cols[i] + ":")
        legend.append(f"{yname}: r^2 = {fit['rsq']:.3f}")
    plt.xlabel("dan rank")
    plt.legend(legend)


def dan(rank):
    rank = rank.lower()
    if rank[-1] in ["d", "p", "段"]:
        return int(rank[:-1])
    elif rank == "?":
        return np.nan
    elif rank == "":
        return np.nan
    else:
        return np.nan
        #assert rank[-1] in ["k", "级"], f"unexpected rank {rank}"
        #return 1 - int(rank[:-1])


def polyfit(x, y, degree=1):
    coeffs = np.polyfit(x, y, degree)
    correlation = np.corrcoef(x, y)[0, 1]
    results = {"coef": coeffs.tolist(), "r": correlation,
               "rsq": correlation ** 2}
    return results


def rankingChiffre(valeur):
    try:
        n = re.findall('\d+', str(valeur))
        k = re.findall('[k|K]+', str(valeur))
        num = 35 if len(n) == 0 else int(n[0])
        rang = num if len(k) == 0 else num*-1+1
    except:
        print(valeur)
        rang = 1
    """
    if not valeur:
        valeur=["35k"]
    if valeur[0][-1]=="k":
        rang=((int(valeur[0][0:-1]))*(-1))+1
    else:
        rang=int(valeur[0][0:-1])
    """
    return rang



def main():
    gopred = GoPredict()
    games = gopred.sgf_analysis()
    df4, y_pred, y_pred_rank = gopred.game_analysis(games, size=19)
    df5 = pd.DataFrame(df4['Pseudo'].tolist(), columns=['name', 'rank', 'oppo_rank', 'color', 'game_id', 'game_name', 'result', 'size', 'game_date'])
    # df_result=pd.concat([pd.Series(df4.Pseudo,name="Pseudo").reset_index(drop=True),pd.Series(y_pred,name="Rang predit").reset_index(drop=True),pd.Series(y_pred_rank,name="Rang nommé").reset_index(drop=True)],axis=1)
    df_result = pd.concat([df5.reset_index(drop=True), pd.Series(y_pred, name="Rang predit").reset_index(drop=True), pd.Series(
        y_pred_rank, name="Rank Normalize").reset_index(drop=True), df4.drop('Pseudo', axis=1).reset_index(drop=True)], axis=1)
    # df_result.to_csv("data/prediction.csv")
    df_result.to_excel("data/prediction.xlsx")
    del df3, df4, df5


if __name__ == "__main__":
    main()
