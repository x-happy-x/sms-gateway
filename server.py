#!/opt/bin/python3
import hashlib,hmac,json,logging,os,re,secrets,sqlite3,threading,time
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from pathlib import Path
from routeros import RouterOS
from pdu import decode,ussd,GSM,EXT
BASE=Path(__file__).resolve().parent
CFG=json.loads((BASE/'config.json').read_text())
LOCK=threading.Lock();STATE={'error':None,'last_sync':None,'used':None,'capacity':None};CSRF=secrets.token_urlsafe(32)
logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
def db():
 c=sqlite3.connect(BASE/'archive.sqlite',timeout=15);c.row_factory=sqlite3.Row;return c
def initialize():
 with db() as c:
  c.executescript('''PRAGMA journal_mode=WAL;
  CREATE TABLE IF NOT EXISTS sms (id INTEGER PRIMARY KEY, hash TEXT UNIQUE, raw TEXT NOT NULL, data TEXT NOT NULL, added REAL NOT NULL);
  CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, kind TEXT, request TEXT, status TEXT, result TEXT, added REAL);
  UPDATE operations SET status='unknown',result='Сервис перезапущен во время операции. Проверьте результат перед повтором.' WHERE status='pending';''')
def read_pdus(r):
 if '+CMGF: 0' not in r.at('AT+CMGF?'):r.at('AT+CMGF=0')
 out=r.at('AT+CMGL=4')
 if not re.search(r'\bOK\b',out):raise RuntimeError('Модем вернул неполный список SMS')
 return [(int(i),raw.upper()) for i,raw in re.findall(r'\+CMGL:\s*(\d+),[^\r\n]+[\r\n]+([0-9A-Fa-f]+)',out)]
def archive(r):
 items=read_pdus(r)
 with db() as c:
  for slot,raw in items:
   try:data=decode(raw)
   except Exception as e:data={'sender':'Неизвестный','text':'Не удалось декодировать: '+str(e),'timestamp':'','concat':None,'decode_error':True}
   c.execute('INSERT OR IGNORE INTO sms(hash,raw,data,added) VALUES(?,?,?,?)',(hashlib.sha256(raw.encode()).hexdigest(),raw,json.dumps(data,ensure_ascii=False),time.time()))
 deleted=release_archived(r,items)
 cpms=r.at('AT+CPMS?');m=re.search(r'\+CPMS:\s*"[^"]+",(\d+),(\d+)',cpms)
 STATE.update(last_sync=time.time(),error=None,auto_deleted=deleted,used=int(m[1]) if m else len(items),capacity=int(m[2]) if m else None)
 return items

def sync():
 with LOCK:
  r=RouterOS(CFG['router'])
  try:archive(r)
  finally:r.close()
def poll():
 while True:
  try:sync()
  except Exception as e:STATE['error']=str(e);logging.warning('SMS sync: %s',e)
  time.sleep(CFG.get('poll_seconds',30))
def messages():
 with db() as c:rows=c.execute('SELECT id,data,added FROM sms ORDER BY added,id').fetchall()
 out=[];groups={}
 for row in rows:
  d=json.loads(row['data']);d.update(id=row['id'],added=row['added']);con=d.get('concat')
  if not con:out.append(d);continue
  ref,total,part=con
  key=(d['sender'],ref,total)
  groups.setdefault(key,[])
  # A reused concatenation reference starts a new group after 24h or a repeated part.
  from datetime import datetime
  try:stamp=datetime.fromisoformat(d['timestamp']).timestamp()
  except Exception:stamp=d['added']
  group=next((g for g in reversed(groups[key]) if part not in g['parts'] and abs(stamp-g['stamp'])<86400),None)
  if group is None:
   group={'base':dict(d),'parts':{},'stamp':stamp};groups[key].append(group)
  group['parts'][part]=d['text']
 for key,gs in groups.items():
  for g in gs:
   d=g['base'];total=key[2];d['text']=''.join(g['parts'].get(i,'\n[Ожидается часть %s]\n'%i) for i in range(1,total+1));d['parts']=f"{len(g['parts'])}/{total}";out.append(d)
 return sorted(out,key=lambda x:(x['timestamp'],x['added']),reverse=True)
def logs(r):return [x for x in r.call('/log/print',query=['?buffer=smsgw']) if '+CUSD:' in x.get('message','') or '+CMGS:' in x.get('message','') or '+CMS ERROR:' in x.get('message','')]
def logkey(x):return (x.get('.id'),x.get('time'),x.get('message'))
def do_ussd(r,code):
 seen={logkey(x) for x in logs(r)}
 out=r.at('AT+CUSD=1,"'+code+'",15',True)
 deadline=time.monotonic()+120
 while True:
  if '+CUSD:' in out:return ussd(out)
  for row in logs(r):
   if logkey(row) not in seen and '+CUSD:' in row.get('message',''):return ussd(row['message'])
  if time.monotonic()>deadline:raise TimeoutError('Ответ USSD не получен за 120 секунд')
  time.sleep(1)
def validate_sms(number,text):
 if not isinstance(number,str) or not re.fullmatch(r'\+?\d{3,15}',number):raise ValueError('Номер: от 3 до 15 цифр, допустим + в начале')
 if not isinstance(text,str) or not text.strip():raise ValueError('Введите текст SMS')
 allowed=set(GSM)-{'\x1b'};extended=set(EXT.values())
 if any(ch not in allowed|extended for ch in text):raise ValueError('RouterOS отправляет только GSM7. Используйте кнопку «В транслит» и проверьте текст перед отправкой.')
 units=sum(2 if ch in extended else 1 for ch in text)
 if units>160:raise ValueError('Максимум 160 единиц GSM7. Символы ^ { } [ ] ~ | \\ и € занимают по 2.')
 return units

def send_sms(r,number,text):
 validate_sms(number,text)
 r.call('/tool/sms/send',**{'port':'lte1','phone-number':number,'message':text,'status-report-request':'no'})
 return {'text':'RouterOS подтвердил отправку SMS. Это не отчёт о доставке.'}
def release_archived(r,items):
 deleted=0
 for slot,raw in items:
  with db() as c:row=c.execute('SELECT raw,data FROM sms WHERE hash=?',(hashlib.sha256(raw.encode()).hexdigest(),)).fetchone()
  if not row or row['raw']!=raw or json.loads(row['data']).get('decode_error'):continue
  # Delete only after the archive transaction committed and the slot still matches.
  current=r.at('AT+CMGR='+str(slot))
  lines={line.strip().upper() for line in current.splitlines()}
  if raw not in lines:continue
  result=r.at('AT+CMGD='+str(slot))
  if not re.search(r'\bOK\b',result):raise RuntimeError('Модем не подтвердил очистку ячейки SIM')
  deleted+=1
 return deleted

def clear_archived(r):
 archive(r)
 return {'text':f"Освобождено ячеек SIM: {STATE.get('auto_deleted',0)}. SMS сохранены в архиве."}
def operate(oid,kind,payload):
 try:
  with LOCK:
   r=RouterOS(CFG['router'])
   try:
    if kind=='ussd':result=do_ussd(r,payload['code'])
    elif kind=='send':result=send_sms(r,payload['number'],payload['text'])
    elif kind=='clear':result=clear_archived(r)
    else:archive(r);result={'text':'SMS обновлены'}
   finally:r.close()
  status='done'
 except Exception as e:status='error';result={'text':str(e)}
 with db() as c:c.execute('UPDATE operations SET status=?,result=? WHERE id=?',(status,json.dumps(result,ensure_ascii=False),oid))

class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def reply(self,code,obj,ctype='application/json; charset=utf-8'):
  b=(json.dumps(obj,ensure_ascii=False).encode() if ctype.startswith('application/json') else obj)
  self.send_response(code);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(b)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('X-Frame-Options','DENY');self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'");self.end_headers();self.wfile.write(b)
 def do_GET(self):
  if self.path=='/':return self.reply(200,(BASE/'index.html').read_bytes(),'text/html; charset=utf-8')
  if self.path=='/api/state':
   with db() as c:ops=[dict(x) for x in c.execute('SELECT * FROM operations ORDER BY added DESC LIMIT 30')]
   for op in ops:
    try:op['result']=json.loads(op['result']) if op['result'] else None
    except ValueError:op['result']={'text':op['result']}
    op['request']=json.loads(op['request'])
   return self.reply(200,dict(state=dict(STATE),messages=messages(),operations=ops,csrf=CSRF))
  return self.reply(404,{'error':'Не найдено'})
 def do_POST(self):
  if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),CSRF):return self.reply(403,{'error':'Обновите страницу'})
  try:
   n=int(self.headers.get('Content-Length','0'))
   if not 0<n<=16384:raise ValueError('Недопустимый размер запроса')
   if not self.headers.get('Content-Type','').startswith('application/json'):raise ValueError('Ожидается JSON')
   p=json.loads(self.rfile.read(n));kind=self.path.removeprefix('/api/');oid=p.get('id','')
   if kind not in ('ussd','send','sync','clear'):return self.reply(404,{'error':'Не найдено'})
   if not isinstance(oid,str) or not re.fullmatch(r'[a-zA-Z0-9-]{16,80}',oid):raise ValueError('Нужен id операции')
   if kind=='ussd' and (not isinstance(p.get('code'),str) or not re.fullmatch(r'[0-9*#]{1,40}',p['code'])):raise ValueError('Допустимы цифры, * и #')
   if kind=='send':validate_sms(p.get('number',''),p.get('text',''))
   if kind=='clear' and p.get('confirm') is not True:raise ValueError('Подтвердите очистку SIM')
   with db() as c:
    c.execute('BEGIN IMMEDIATE')
    old=c.execute('SELECT id FROM operations WHERE id=?',(oid,)).fetchone()
    if old:return self.reply(200,{'id':oid})
    if c.execute("SELECT 1 FROM operations WHERE status='pending'").fetchone():return self.reply(409,{'error':'Дождитесь завершения текущей операции'})
    c.execute('INSERT INTO operations VALUES(?,?,?,?,?,?)',(oid,kind,json.dumps(p,ensure_ascii=False),'pending',None,time.time()))
   threading.Thread(target=operate,args=(oid,kind,p),daemon=True).start();self.reply(202,{'id':oid})
  except (ValueError,TypeError,KeyError) as e:self.reply(400,{'error':str(e)})
  except Exception:logging.exception('HTTP operation');self.reply(500,{'error':'Внутренняя ошибка сервиса'})
if __name__=='__main__':
 os.umask(0o077);initialize();threading.Thread(target=poll,daemon=True).start()
 server=ThreadingHTTPServer((CFG.get('listen','192.168.1.1'),CFG.get('port',8099)),Handler)
 logging.info('SMS Gateway listening on %s:%s',*server.server_address);server.serve_forever()
