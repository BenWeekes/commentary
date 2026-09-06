"""5 random non-overlapping 5-min in-play windows from the full Mainz-Union broadcast."""
import json, random, subprocess
SRC='/home/ubuntu/commentary/clips/md33_full/soccer_germany_bundesliga_8521005_3064k.mp4'
OUT='/var/www/html/experiments/ai_commentator/md33_clips'
# calibrated: 1H file = clock+1797 (10:03 @ 2403); 2H file = clock+3478 (58:42 @ 7000)
RANGES=[(1850,4550,1797,'1'),(6200,8630,3478,'2')]   # in-play, keep 300s headroom
GOALS=[(37*60,0,1),(48*60,1,1),(88*60,1,2),(90*60+30,1,3)]  # clock_s -> score AFTER
def score_at(clock_s):
    h=a=0
    for g,hh,aa in GOALS:
        if clock_s>g: h,a=hh,aa
    return h,a
random.seed(33)
wins=[]
while len(wins)<5:
    lo,hi,off,per=random.choice(RANGES)
    s=random.randint(lo,hi-300)
    if any(abs(s-w['file_s'])<330 for w in wins): continue
    clock=s-off
    h,a=score_at(clock)
    wins.append({'file_s':s,'period':per,'clock':f"{clock//60}:{clock%60:02d}",'home':h,'away':a})
wins.sort(key=lambda w:w['file_s'])
import pathlib; pathlib.Path(OUT).mkdir(exist_ok=True)
for i,w in enumerate(wins,1):
    w['name']=f"r{i}_clock{w['clock'].replace(':','m')}.mp4"
    print(f"r{i}: period {w['period']} clock {w['clock']} score {w['home']}-{w['away']} (file {w['file_s']}s)")
    subprocess.run(['nice','-n','15','ffmpeg','-y','-v','error','-threads','2','-ss',str(w['file_s']),
        '-i',SRC,'-t','300','-c:v','libx264','-preset','veryfast','-crf','20',
        '-c:a','aac','-b:a','128k',f"{OUT}/{w['name']}"],check=True)
    print(f"  done -> {w['name']}", flush=True)
json.dump(wins, open(f"{OUT}/windows.json",'w'), indent=1)
json.dump(wins, open('random_windows.json','w'), indent=1)
print("ALL DONE")
