"""Additional DeadAir media and Roblox IP tools."""
import asyncio
import io
import math
import re
from collections import Counter
from datetime import datetime, timezone
from PIL import Image, ImageOps, ImageDraw, ExifTags
import discord
from discord import app_commands

MAX_INPUT = 20 * 1024 * 1024

async def read_attachment(att):
    if att.size > MAX_INPUT:
        raise ValueError('Input must be 20 MB or smaller.')
    return await att.read()

async def ffmpeg(data, args, suffix='wav'):
    proc = await asyncio.create_subprocess_exec('ffmpeg','-hide_banner','-loglevel','error','-nostdin','-i','pipe:0',*args,'-f',suffix,'pipe:1',stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:
        out,err = await asyncio.wait_for(proc.communicate(data),timeout=100)
    except asyncio.TimeoutError:
        proc.kill(); await proc.communicate(); raise ValueError('Audio processing timed out.')
    if proc.returncode or not out:
        raise ValueError('FFmpeg could not process that audio. '+err.decode(errors='replace')[-300:])
    if len(out)>24*1024*1024: raise ValueError('Output too large for Discord. Try a shorter file.')
    return discord.File(io.BytesIO(out),filename='deadair.'+suffix)

def register(bot):
    async def execute(interaction, operation):
        await interaction.response.defer(thinking=True)
        try:
            result=await operation()
            if isinstance(result,discord.File): await interaction.followup.send(file=result)
            elif isinstance(result,tuple): await interaction.followup.send(**result[1])
            else: await interaction.followup.send(result)
        except (ValueError, OSError, discord.HTTPException) as e:
            await interaction.followup.send('Error: '+str(e)[:1500],ephemeral=True)

    @bot.tree.command(name='dmca',description='Prepare a Roblox copyright takedown notice draft (not submitted).')
    @app_commands.describe(asset_id='Roblox asset ID',owner='Your legal name or copyright owner',work='Description of your original copyrighted work',claim='How this Roblox asset infringes your work',contact='Your contact email for the notice')
    async def dmca(interaction:discord.Interaction,asset_id:str,owner:str,work:str,claim:str,contact:str):
        if not asset_id.isdecimal() or len(asset_id)>20:
            await interaction.response.send_message('Please enter a valid numeric asset ID.',ephemeral=True);return
        body=(f'Copyright takedown notice — DRAFT ONLY\n\nRoblox asset: https://www.roblox.com/library/{asset_id}\nCopyright owner: {owner}\nOriginal copyrighted work: {work}\nClaim of infringement: {claim}\nContact: {contact}\n\n'
              'Required declarations to review and affirm if true:\n'
              'I have a good-faith belief that the disputed use is not authorized by the copyright owner, its agent, or the law.\n'
              'I state under penalty of perjury that the information in this notice is accurate and that I am the copyright owner or authorized to act on the owner’s behalf.\n\n'
              'Electronic signature: [ADD YOUR SIGNATURE]\nDate: '+datetime.now(timezone.utc).strftime('%Y-%m-%d')+'\n\n'
              'Review accuracy, add your mailing address and signature, and submit through Roblox’s official copyright process. No notice has been sent.')
        await interaction.response.send_message(file=discord.File(io.BytesIO(body.encode()),filename='dmca_draft.txt'),ephemeral=True)

    @bot.tree.command(name='normalize',description='Normalize audio to a target integrated loudness.')
    async def normalize(interaction:discord.Interaction,file:discord.Attachment,target_lufs:app_commands.Range[float,-30,-8]=-16):
        async def run(): return await ffmpeg(await read_attachment(file),['-af',f'loudnorm=I={target_lufs}:TP=-1.5:LRA=11','-acodec','pcm_s16le'])
        await execute(interaction,run)

    @bot.tree.command(name='speed',description='Change audio playback speed while preserving pitch.')
    async def speed(interaction:discord.Interaction,file:discord.Attachment,multiplier:app_commands.Range[float,0.25,4.0]):
        async def run():
            n=float(multiplier); filters=[]
            while n>2: filters.append('atempo=2');n/=2
            while n<0.5: filters.append('atempo=0.5');n*=2
            filters.append(f'atempo={n:.6f}')
            return await ffmpeg(await read_attachment(file),['-af',','.join(filters),'-acodec','pcm_s16le'])
        await execute(interaction,run)

    @bot.tree.command(name='pitch',description='Shift audio pitch in semitones, preserving duration.')
    async def pitch(interaction:discord.Interaction,file:discord.Attachment,semitones:app_commands.Range[float,-12,12]):
        async def run():
            factor=2**(float(semitones)/12)
            # Resample to a fixed rate, then compensate tempo for pitch shift.
            return await ffmpeg(await read_attachment(file),['-af',f'asetrate=44100*{factor:.8f},aresample=44100,atempo={1/factor:.8f}','-ar','44100','-acodec','pcm_s16le'])
        await execute(interaction,run)

    @bot.tree.command(name='silence',description='Trim quiet audio from the beginning and end.')
    async def silence(interaction:discord.Interaction,file:discord.Attachment,threshold_db:app_commands.Range[float,-80,-10]=-45):
        async def run():
            # Reverse and trim again so interior pauses remain untouched.
            af=f'silenceremove=start_periods=1:start_duration=0:start_threshold={threshold_db}dB,areverse,silenceremove=start_periods=1:start_duration=0:start_threshold={threshold_db}dB,areverse'
            return await ffmpeg(await read_attachment(file),['-af',af,'-acodec','pcm_s16le'])
        await execute(interaction,run)

    @bot.tree.command(name='palette',description='Extract dominant colors and HEX swatches from an image.')
    async def palette(interaction:discord.Interaction,file:discord.Attachment,colors:app_commands.Range[int,2,10]=6):
        async def run():
            im=Image.open(io.BytesIO(await read_attachment(file))).convert('RGB');im.thumbnail((300,300))
            quant=im.quantize(colors=colors,method=Image.Quantize.MEDIANCUT)
            values=sorted(quant.getcolors() or [],reverse=True)[:colors]
            pal=quant.getpalette(); sw=Image.new('RGB',(500,len(values)*60),'white');draw=ImageDraw.Draw(sw)
            lines=[]
            for i,(_,idx) in enumerate(values):
                rgb=tuple(pal[idx*3:idx*3+3]);hexcode='#%02X%02X%02X'%rgb
                draw.rectangle((0,i*60,120,(i+1)*60),fill=rgb)
                draw.text((140,i*60+20),hexcode,fill='black');lines.append(hexcode)
            out=io.BytesIO();sw.save(out,'PNG');out.seek(0)
            return ('palette',{'content':'Dominant colors: '+', '.join(lines),'file':discord.File(out,filename='palette.png')})
        await execute(interaction,run)

    @bot.tree.command(name='imageinfo',description='Inspect image dimensions, format, EXIF, or strip metadata.')
    async def imageinfo(interaction:discord.Interaction,file:discord.Attachment,strip:bool=False):
        async def run():
            im=Image.open(io.BytesIO(await read_attachment(file)))
            if im.width*im.height>60_000_000: raise ValueError('Image dimensions too large.')
            if strip:
                # Reconstruct pixels in a new image to avoid copying EXIF, comments, and text chunks.
                im=ImageOps.exif_transpose(im)
                if im.mode not in ('RGB','RGBA'):im=im.convert('RGBA')
                clean=Image.new(im.mode,im.size);clean.paste(im)
                out=io.BytesIO();clean.save(out,format='PNG');out.seek(0)
                return discord.File(out,filename='metadata_removed.png')
            exif=im.getexif();fields=[]
            for k,v in list(exif.items())[:15]:
                key=ExifTags.TAGS.get(k,str(k))
                if key=='GPSInfo': v='[location data present — hidden]'
                fields.append(f'{key}: {str(v)[:100]}')
            return f'**{im.width} × {im.height}** pixels | **{im.format}** | **{im.mode}**\nEXIF tags: {len(exif)}\n'+('\n'.join(fields) if fields else 'No EXIF tags found.')
        await execute(interaction,run)

    @bot.tree.command(name='emoji',description='Resize an image to a 128×128 transparent Discord emoji PNG.')
    async def emoji(interaction:discord.Interaction,file:discord.Attachment):
        async def run():
            im=Image.open(io.BytesIO(await read_attachment(file))).convert('RGBA')
            if im.width*im.height>60_000_000: raise ValueError('Image dimensions too large.')
            im.thumbnail((128,128),Image.Resampling.LANCZOS)
            canvas=Image.new('RGBA',(128,128),(0,0,0,0));canvas.alpha_composite(im,((128-im.width)//2,(128-im.height)//2))
            out=io.BytesIO();canvas.save(out,'PNG',optimize=True);out.seek(0)
            return discord.File(out,filename='emoji.png')
        await execute(interaction,run)
