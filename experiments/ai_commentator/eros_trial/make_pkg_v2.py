#!/usr/bin/env python3
"""match_package in the vendor's REAL schema (eros.match-package.v1), from sr_cache."""
import json, sys
ZH={'Zentner':'岑特纳','da Costa':'达科斯塔','Posch':'波施','Kohr':'科尔','Caci':'卡奇',
'Sano':'佐野海舟','Mwene':'姆韦内','Nebel':'内贝尔','Amiri':'阿米里','Tietz':'蒂茨',
'Becker':'贝克尔','Weiper':'魏佩尔','Potulski':'波图尔斯基','Veratschnig':'韦拉奇尼格',
'Kawasaki':'川崎飒太','Sieb':'西布','Maloney':'马洛尼','Jae-sung':'李在城','Widmer':'维德默',
'Batz':'巴茨','Klaus':'克劳斯','Trimmel':'特里梅尔','Leite':'莱特','Doekhi':'杜基',
'Rothe':'罗特','Khedira':'赫迪拉','Kemlein':'凯姆莱因','Burke':'伯克','Ansah':'安萨',
'Burcu':'布尔库','Ilic':'伊利奇','Wisbereit':'维斯贝赖特','Schafer':'舍费尔',
'Querfeld':'奎尔费尔德','Woo-yeong':'郑优营','Kohn':'科恩','Nsoki':'恩索基',
'Skarke':'斯卡尔克','Juranovic':'尤拉诺维奇','Kral':'克拉尔'}
sr=json.load(open('/home/ubuntu/commentary/match_data/m05_uni_md33/sr_cache.json'))
CODE={'M05':'M05','UNI':'FCU'}   # broadcast scoreboard codes (verified on screen: M05 / FCU)
roster=[]
for c in sr['lineups']['lineups']['competitors']:
    code=CODE[c['abbreviation']]
    for p in c['players']:
        sur=p['name'].split(',')[0].strip()
        first=p['name'].split(',')[1].strip() if ',' in p['name'] else ''
        latin=(first+' '+sur).strip()
        roster.append({'team':code,'number':p['jersey_number'],
            'name_zh':ZH.get(sur,sur),'name':latin,
            'name_fr':latin,'name_es':latin,'name_pt-br':latin,'name_tr':latin})
pkg={
 'schema_version':'eros.match-package.v1',
 'competition':'Bundesliga','venue':'Mewa Arena, Mainz',
 'phase':'下半場','score_at_clip_start':[1,1],'kickoff_clock':'76:50',
 'team_sides':{'M05':'home','FCU':'away'},
 'teams':{
   'M05':{'name_zh':'美因茨','name':'FSV Mainz 05','name_fr':'Mayence','name_es':'Maguncia',
          'name_pt-br':'Mainz','name_tr':'Mainz','kit':'红色',
          'goalkeeper_kit':'青绿色','goalkeeper_kit_terms':['青绿色','湖蓝色','浅绿色','蓝绿色'],
          'coach':'Urs Fischer'},
   'FCU':{'name_zh':'柏林联合','name':'Union Berlin','name_fr':'Union Berlin','name_es':'Union Berlin',
          'name_pt-br':'Union Berlin','name_tr':'Union Berlin','kit':'墨绿色',
          'goalkeeper_kit':'亮黄色','goalkeeper_kit_terms':['亮黄色','黄色','荧光黄'],
          'coach':'Marie-Louise Eta'}},
 'roster_entities':roster,
 'verified_context':[
  "两队在该片段开始时战成1比1。",
  "柏林联合近9场德甲对阵美因茨保持不败，这是他们德甲面对单一对手的最长不败纪录。",
  "2026年1月的首回合，美因茨一度2比0领先，柏林联合在最后15分钟连进两球追成2比2。",
  "美因茨主帅费舍尔2018至2023年执教柏林联合，带队从德乙升入德甲并一路进入欧冠。",
  "两队都在上一个比赛日提前完成保级，本场是赛季倒数第二轮，也是美因茨本赛季最后一个主场。",
  "费舍尔2025年12月接手美因茨时球队排名垫底，本场前已经提前两轮完成保级。",
  "柏林联合目前由玛丽-路易丝·埃塔以临时主帅身份带队至赛季结束。",
  "美因茨上轮客场2比1击败圣保利，并凭借这场胜利提前两轮保级。",
  "费舍尔赛前特别提醒美因茨避免送给柏林联合过多定位球机会，后者本赛季多次依靠定位球得分。",
  "费舍尔赛前认为柏林联合锋线速度突出，攻防转换尤其危险。"],
 'forbidden_topics':[]}
json.dump(pkg, open('pkg_std_v2.json','w'), ensure_ascii=False, indent=1)
print(f"pkg_std_v2.json: {len(roster)} roster entries, codes M05/FCU, context 10 lines")
