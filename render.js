const puppeteer = require('puppeteer');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

(async () => {
    console.log('[1/4] Launching Headless Chromium...');
    const browser = await puppeteer.launch({
        headless: 'new',
        args: [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--font-render-hinting=none'
        ]
    });

    const page = await browser.newPage();
    await page.setViewport({ width: 1080, height: 1920, deviceScaleFactor: 1 });

    const htmlPath = 'file://' + path.resolve(__dirname, 'reel.html');
    await page.goto(htmlPath, { waitUntil: 'networkidle0' });

    // Ensure typography is fully loaded before capture
    await page.evaluate(() => document.fonts.ready);

    const frameDir = path.join(__dirname, 'frames');
    if (!fs.existsSync(frameDir)) fs.mkdirSync(frameDir);

    console.log('[2/4] Synthesizing Web Audio Track via OfflineAudioContext...');
    const wavBase64 = await page.evaluate(async () => {
        const wavUint8 = await window.synthesizeAudioWav();
        let binary = '';
        for (let i = 0; i < wavUint8.byteLength; i++) {
            binary += String.fromCharCode(wavUint8[i]);
        }
        return btoa(binary);
    });

    const audioPath = path.join(__dirname, 'audio.wav');
    fs.writeFileSync(audioPath, Buffer.from(wavBase64, 'base64'));

    console.log('[3/4] Capturing 30 FPS High-Res Animation Frames...');
    const FPS = 30;
    const DURATION = 13.5;
    const TOTAL_FRAMES = Math.round(FPS * DURATION);

    for (let f = 0; f < TOTAL_FRAMES; f++) {
        await page.evaluate((frameIdx) => {
            window.renderFrame(frameIdx);
        }, f);

        const canvasElement = await page.$('#mainCanvas');
        const frameNum = String(f).padStart(5, '0');
        await canvasElement.screenshot({
            path: path.join(frameDir, `frame_${frameNum}.png`),
            omitBackground: false
        });

        if (f % 60 === 0 || f === TOTAL_FRAMES - 1) {
            console.log(` Rendered ${f + 1}/${TOTAL_FRAMES} frames (${Math.round(((f + 1) / TOTAL_FRAMES) * 100)}%)`);
        }
    }

    await browser.close();

    console.log('[4/4] Encoding Final Instagram Reel via FFmpeg...');
    const outputVideo = path.join(__dirname, 'stamp_wear_reel.mp4');

    const ffmpegArgs = [
        '-y',
        '-framerate', '30',
        '-i', path.join(frameDir, 'frame_%05d.png'),
        '-i', audioPath,
        '-c:v', 'libx264',
        '-preset', 'slow',
        '-crf', '18',
        '-pix_fmt', 'yuv420p',
        '-c:a', 'aac',
        '-b:a', '192k',
        '-movflags', '+faststart',
        outputVideo
    ];

    const ffmpeg = spawn('ffmpeg', ffmpegArgs);
    ffmpeg.stderr.on('data', (d) => process.stdout.write(''));
    ffmpeg.on('close', (code) => {
        if (code === 0) {
            console.log(`\n🎉 SUCCESS! Rendered file saved to: ${outputVideo}`);
        } else {
            console.error(`FFmpeg process failed with code ${code}`);
            process.exit(1);
        }
    });
})();
