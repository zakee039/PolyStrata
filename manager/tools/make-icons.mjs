import fs from "node:fs";
import zlib from "node:zlib";
import path from "node:path";
import { fileURLToPath } from "node:url";
const dir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../src-tauri/icons");
fs.mkdirSync(dir, {recursive:true});
const width=128,height=128;
const pix=Buffer.alloc(width*height*4,0);
function pixel(x,y,r,g,b,a=255){if(x<0||y<0||x>=width||y>=height)return;const p=(y*width+x)*4;pix[p]=r;pix[p+1]=g;pix[p+2]=b;pix[p+3]=a;}
function within(x,y,x0,y0,x1,y1,rad){if(x>=x0+rad&&x<=x1-rad&&y>=y0&&y<=y1)return true;if(y>=y0+rad&&y<=y1-rad&&x>=x0&&x<=x1)return true;const px=x<x0+rad?x0+rad:x1-rad,py=y<y0+rad?y0+rad:y1-rad;return (x-px)**2+(y-py)**2<=rad**2;}
for(let y=0;y<128;y++)for(let x=0;x<128;x++){
 const inside=within(x,y,5,5,122,122,25);if(!inside)continue;
 pixel(x,y,57,197,187);
 const p=(x,y,x0,y0,x1,y1)=>x>=x0&&x<=x1&&y>=y0&&y<=y1;
 const letterP=(p(x,y,34,30,45,94)||p(x,y,34,30,76,40)||p(x,y,73,40,84,58)||p(x,y,34,57,76,68));
 const line=(p(x,y,45,57,73,68)||p(x,y,34,88,46,94));
 if(letterP||line)pixel(x,y,255,255,255);
}
const crcTable=new Uint32Array(256);for(let i=0;i<256;i++){let c=i;for(let k=0;k<8;k++)c=c&1?0xedb88320^(c>>>1):c>>>1;crcTable[i]=c>>>0;}
function crc(buf){let c=0xffffffff;for(const b of buf)c=crcTable[(c^b)&255]^(c>>>8);return (c^0xffffffff)>>>0;}
function chunk(tag,data){const t=Buffer.from(tag);const len=Buffer.alloc(4);len.writeUInt32BE(data.length);const end=Buffer.alloc(4);end.writeUInt32BE(crc(Buffer.concat([t,data])));return Buffer.concat([len,t,data,end]);}
const ihdr=Buffer.alloc(13);ihdr.writeUInt32BE(width,0);ihdr.writeUInt32BE(height,4);ihdr[8]=8;ihdr[9]=6;
const scan=Buffer.alloc((width*4+1)*height);for(let y=0;y<height;y++)pix.copy(scan,y*(width*4+1)+1,y*width*4,(y+1)*width*4);
const png=Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]),chunk("IHDR",ihdr),chunk("IDAT",zlib.deflateSync(scan)),chunk("IEND",Buffer.alloc(0))]);
fs.writeFileSync(path.join(dir,"icon.png"),png);
// Windows RC.exe accepts the classic ICO DIB form more consistently than PNG-in-ICO.
const iw=64,ih=64,maskRow=8;
const dib=Buffer.alloc(40+iw*ih*4+maskRow*ih);
dib.writeUInt32LE(40,0);dib.writeInt32LE(iw,4);dib.writeInt32LE(ih*2,8);
dib.writeUInt16LE(1,12);dib.writeUInt16LE(32,14);dib.writeUInt32LE(iw*ih*4+maskRow*ih,20);
for(let y=0;y<ih;y++)for(let x=0;x<iw;x++){
 const p=((ih-1-y)*2*width+x*2)*4;
 const q=40+(y*iw+x)*4;
 dib[q]=pix[p+2];dib[q+1]=pix[p+1];dib[q+2]=pix[p];dib[q+3]=pix[p+3];
}
const header=Buffer.alloc(22);header.writeUInt16LE(0,0);header.writeUInt16LE(1,2);header.writeUInt16LE(1,4);
header[6]=iw;header[7]=ih;header[8]=0;header[9]=0;header.writeUInt16LE(1,10);header.writeUInt16LE(32,12);header.writeUInt32LE(dib.length,14);header.writeUInt32LE(22,18);
fs.writeFileSync(path.join(dir,"icon.ico"),Buffer.concat([header,dib]));
console.log("Icon PNG/ICO ready",png.length,dib.length);
