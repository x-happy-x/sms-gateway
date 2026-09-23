"""GSM 03.38 / 03.40 SMS-DELIVER and UCS2 SMS-SUBMIT."""
import math,re,secrets,datetime
GSM='@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !"#¤%&\'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà'
EXT={10:'\f',20:'^',40:'{',41:'}',47:'\\',60:'[',61:'~',62:']',64:'|',101:'€'}
def gsm7(data,count,offset=0):
 n=int.from_bytes(data,'little');out='';esc=False
 for i in range(count):
  v=(n>>(offset+7*i))&127
  if esc:out+=EXT.get(v,'�');esc=False
  elif v==27:esc=True
  else:out+=GSM[v]
 return out

def semi(data):return ''.join(f'{b&15:X}{b>>4:X}' for b in data)
def decode(raw):
 b=bytes.fromhex(raw);p=1+b[0]
 def take(n):
  nonlocal p
  if p+n>len(b):raise ValueError('Обрезанный PDU')
  v=b[p:p+n];p+=n;return v
 first=take(1)[0]
 if first&3!=0:raise ValueError('Не SMS-DELIVER')
 size,toa=take(2);addr=take((size+1)//2)
 sender=gsm7(addr,size*4//7) if toa&0x70==0x50 else ('+' if toa&0x70==0x10 else '')+semi(addr)[:size]
 pid,dcs=take(2);stamp=take(7)
 ds=[int(semi(bytes([x]))) for x in stamp[:6]]
 z=stamp[6];zone=((z&7)*10+(z>>4))*15*(-1 if z&8 else 1)
 timestamp=datetime.datetime(2000+ds[0],*ds[1:],tzinfo=datetime.timezone(datetime.timedelta(minutes=zone))).isoformat()
 udl=take(1)[0];data=b[p:];header=0;concat=None
 if first&64:
  if not data or data[0]+1>len(data):raise ValueError('Обрезанный UDH')
  header=data[0]+1;j=1
  while j+2<=header:
   ie,n=data[j:j+2];val=data[j+2:j+2+n];j+=2+n
   if len(val)!=n:raise ValueError('Некорректный UDH')
   if ie==0 and n==3:concat=list(val)
   elif ie==8 and n==4:concat=[int.from_bytes(val[:2],'big'),val[2],val[3]]
 if dcs&0xc0==0 and dcs&32:raise ValueError('Сжатый SMS не поддерживается')
 alphabet=(dcs>>2)&3 if dcs<192 else (2 if dcs&0xf0==0xe0 else (1 if dcs&0xf4==0xf4 else 0))
 if alphabet==2:
  if len(data)<udl:raise ValueError('Обрезанный UCS2')
  body=data[header:udl].decode('utf-16-be')
 elif alphabet==0:
  skip=math.ceil(header*8/7)
  if len(data)*8<udl*7:raise ValueError('Обрезанный GSM7')
  body=gsm7(data,udl-skip,skip*7)
 else:body='[Бинарное SMS] '+data[header:udl].hex().upper()
 return dict(sender=sender,text=body,timestamp=timestamp,concat=concat,dcs=dcs)

def encode(number,text):
 if not re.fullmatch(r'\+?\d{3,15}',number):raise ValueError('Номер: от 3 до 15 цифр, допустим + в начале')
 if not text or not text.strip():raise ValueError('Введите текст SMS')
 if any(0xD800<=ord(c)<=0xDFFF or ord(c)>0xFFFF for c in text):raise ValueError('Эмодзи вне UCS2 не поддерживаются; используйте текст без них')
 if len(text)>670:raise ValueError('Максимум 670 символов (10 частей)')
 chunks=[text] if len(text)<=70 else [text[i:i+67] for i in range(0,len(text),67)]
 digits=number.lstrip('+');padded=digits+('F' if len(digits)%2 else '')
 address=bytes.fromhex(''.join(padded[i+1]+padded[i] for i in range(0,len(padded),2)))
 ref=secrets.randbelow(256);result=[]
 for i,part in enumerate(chunks):
  ud=(bytes([5,0,3,ref,len(chunks),i+1]) if len(chunks)>1 else b'')+part.encode('utf-16-be')
  tp=bytes([0x41 if len(chunks)>1 else 1,0,len(digits),0x91 if number.startswith('+') else 0x81])+address+bytes([0,8,len(ud)])+ud
  result.append((len(tp),(b'\0'+tp).hex().upper()))
 return result

def ussd(raw):
 m=re.search(r'\+CUSD:\s*(\d+)(?:\s*,\s*"([^"]*)"\s*(?:,\s*(\d+))?)?',raw)
 if not m:raise ValueError('Нет ответа CUSD')
 status=int(m[1]);body=m[2];dcs=int(m[3] or 15)
 if body is None:return {'text':{2:'Сессия завершена',4:'USSD не поддерживается',5:'Сеть не ответила'}.get(status,'Ответ без текста'),'status':status}
 if re.fullmatch(r'(?:[0-9A-Fa-f]{2})+',body):
  data=bytes.fromhex(body)
  if dcs==72 or dcs&12==8:body=data.decode('utf-16-be')
  elif dcs in (0,15):body=gsm7(data,len(data)*8//7).rstrip('\r')
 return {'text':body,'status':status}
