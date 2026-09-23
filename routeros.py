import socket,struct
class RouterOS:
 def __init__(self,c):
  self.s=socket.create_connection((c['host'],c.get('api_port',8728)),10);self.s.settimeout(190)
  try:self.call('/login',name=c['user'],password=c['password'])
  except: self.close();raise
 def close(self):self.s.close()
 def read(self,n):
  b=b''
  while len(b)<n:
   x=self.s.recv(n-len(b))
   if not x:raise ConnectionError('RouterOS закрыл соединение')
   b+=x
  return b
 def word(self):
  n=self.read(1)[0]
  if n&0x80==0:pass
  elif n&0xc0==0x80:n=((n&63)<<8)+self.read(1)[0]
  elif n&0xe0==0xc0:n=((n&31)<<16)+int.from_bytes(self.read(2),'big')
  elif n&0xf0==0xe0:n=((n&15)<<24)+int.from_bytes(self.read(3),'big')
  elif n==0xf0:n=int.from_bytes(self.read(4),'big')
  else:raise ValueError('Invalid API length')
  if n>8*1024*1024:raise ValueError('API response too large')
  return self.read(n).decode('utf-8','replace') if n else ''
 def call(self,cmd,query=(),**kw):
  words=[cmd]+['='+k+'='+str(v) for k,v in kw.items()]+list(query)
  data=b''
  for w in words:
   b=w.encode();n=len(b)
   p=bytes([n]) if n<128 else struct.pack('!H',n|0x8000) if n<16384 else (n|0xc00000).to_bytes(3,'big') if n<2097152 else struct.pack('!I',n|0xe0000000)
   data+=p+b
  self.s.sendall(data+b'\0');rows=[];err=None
  while True:
   row=[]
   while True:
    w=self.word()
    if not w:break
    row.append(w)
   if not row:continue
   d=dict(w[1:].split('=',1) for w in row[1:] if w.startswith('=') and '=' in w[1:])
   if row[0] in ('!trap','!fatal'):err=d.get('message',str(row))
   if row[0]=='!re':rows.append(d)
   if row[0] in ('!done','!fatal'):
    if err:raise RuntimeError(err)
    if d:rows.append(d)
    return rows
 def at(self,command,wait=False):
  r=self.call('/interface/lte/at-chat',**{'number':'lte1','input':command,'wait':'yes' if wait else 'no'})
  out='\n'.join(x.get('output','') for x in r)
  if 'ERROR' in out:raise RuntimeError(out.strip())
  return out
