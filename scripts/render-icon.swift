// Renders the Scene app icon: a glowing four-point sparkle (long rays, short
// diagonal rays, bright core) in butter on coral. Colors match Theme.swift.
// Usage (from repo root): swiftc -O scripts/render-icon.swift -o /tmp/render-icon && /tmp/render-icon src/ios/VexoBridge/Assets.xcassets/AppIcon.appiconset/AppIcon.png
import AppKit
import ImageIO

let S: CGFloat = 1024
let coral = CGColor(srgbRed: 0xEC/255, green: 0x6A/255, blue: 0x63/255, alpha: 1)
let butter = CGColor(srgbRed: 0xF7/255, green: 0xD2/255, blue: 0x7E/255, alpha: 1)
let cream = CGColor(srgbRed: 1, green: 0.97, blue: 0.86, alpha: 1)

/// Four-point sparkle with concave sides; smaller pinch = thinner rays.
func star(center c: CGPoint, radius r: CGFloat, pinch k: CGFloat, rotation: CGFloat = 0) -> CGPath {
    let p = CGMutablePath()
    let pts = (0..<4).map { i -> CGPoint in
        let a = rotation + CGFloat(i) * .pi / 2 + .pi / 2
        return CGPoint(x: c.x + cos(a) * r, y: c.y + sin(a) * r)
    }
    p.move(to: pts[0])
    // Cubic whose handles sit near the center along each ray: the sides hug the
    // axes, so the rays come out needle-thin (pinch = handle distance / radius).
    for i in 0..<4 {
        let a = pts[i], b = pts[(i + 1) % 4]
        p.addCurve(to: b,
                   control1: CGPoint(x: c.x + (a.x - c.x) * k, y: c.y + (a.y - c.y) * k),
                   control2: CGPoint(x: c.x + (b.x - c.x) * k, y: c.y + (b.y - c.y) * k))
    }
    p.closeSubpath()
    return p
}

let ctx = CGContext(data: nil, width: Int(S), height: Int(S), bitsPerComponent: 8, bytesPerRow: 0,
                    space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)!
let c = CGPoint(x: S / 2, y: S / 2)
let space = CGColorSpace(name: CGColorSpace.sRGB)!

// Background: coral, a touch lighter toward the center so the glow has somewhere to live.
ctx.setFillColor(coral); ctx.fill(CGRect(x: 0, y: 0, width: S, height: S))
let bg = CGGradient(colorsSpace: space, colors: [CGColor(srgbRed: 0xF2/255, green: 0x82/255, blue: 0x72/255, alpha: 1), coral] as CFArray, locations: [0, 1])!
ctx.drawRadialGradient(bg, startCenter: c, startRadius: 0, endCenter: c, endRadius: S * 0.62, options: [])

// Glow behind the sparkle.
let glow = CGGradient(colorsSpace: space, colors: [butter.copy(alpha: 0.75)!, butter.copy(alpha: 0)!] as CFArray, locations: [0, 1])!
ctx.drawRadialGradient(glow, startCenter: c, startRadius: 0, endCenter: c, endRadius: S * 0.30, options: [])

func fillStar(_ path: CGPath, blur: CGFloat) {
    ctx.saveGState()
    ctx.setShadow(offset: .zero, blur: blur, color: butter.copy(alpha: 0.9))
    ctx.addPath(path); ctx.setFillColor(butter); ctx.fillPath()
    ctx.restoreGState()
    // Cream-to-butter shading inside the rays: brighter toward the core.
    ctx.saveGState()
    ctx.addPath(path); ctx.clip()
    let shade = CGGradient(colorsSpace: space, colors: [cream, butter] as CFArray, locations: [0, 1])!
    ctx.drawRadialGradient(shade, startCenter: c, startRadius: 0, endCenter: c, endRadius: S * 0.36, options: [.drawsAfterEndLocation])
    ctx.restoreGState()
}

// Short diagonal rays first, long rays on top.
fillStar(star(center: c, radius: S * 0.24, pinch: 0.10, rotation: .pi / 4), blur: 22)
fillStar(star(center: c, radius: S * 0.41, pinch: 0.14), blur: 40)

// Hot core.
let core = CGGradient(colorsSpace: space, colors: [CGColor(srgbRed: 1, green: 1, blue: 0.97, alpha: 1), cream.copy(alpha: 0)!] as CFArray, locations: [0, 1])!
ctx.drawRadialGradient(core, startCenter: c, startRadius: 0, endCenter: c, endRadius: S * 0.11, options: [])

let dest = CGImageDestinationCreateWithURL(URL(fileURLWithPath: CommandLine.arguments[1]) as CFURL, "public.png" as CFString, 1, nil)!
CGImageDestinationAddImage(dest, ctx.makeImage()!, nil)
CGImageDestinationFinalize(dest)
print("wrote", CommandLine.arguments[1])
