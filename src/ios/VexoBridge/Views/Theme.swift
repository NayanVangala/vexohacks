//
//  Theme.swift
//  VexoBridge
//
//  Every color, radius, and type style the app uses. Placeholder palette
//  (warm canvas, pastel stacked cards, coral action) until Scene's real theme
//  lands: change values here, nothing else needs to move.
//

import SwiftUI

enum Theme {
    // Surfaces
    static let canvas    = Color(hex: 0xE6E3D8)   // warm greige page
    static let card      = Color(hex: 0xFFFFFF)
    static let butter    = Color(hex: 0xF7E1AE)
    static let mint      = Color(hex: 0xC4E9A8)
    static let rose      = Color(hex: 0xF0AEC4)
    static let sky       = Color(hex: 0xEEF1FA)
    static let pastels   = [sky, mint, rose, butter]

    // Ink + accents
    static let ink       = Color(hex: 0x15130F)
    static let inkSoft   = Color(hex: 0x15130F).opacity(0.55)
    static let inkFaint  = Color(hex: 0x15130F).opacity(0.3)
    static let action    = Color(hex: 0xEC6A63)   // coral: the one thing to press
    static let star      = Color(hex: 0xF5CF7F)
    static let chart     = Color(hex: 0x7BD35A)
    static let alert     = Color(hex: 0xEC6A63)

    // Shape
    static let cardRadius: CGFloat = 36
    static let overlap: CGFloat = 34              // how far each card tucks under the next

    // Type
    static func number(_ size: CGFloat) -> Font { .system(size: size, weight: .semibold).monospacedDigit() }
    static let eyebrow = Font.system(size: 11, weight: .medium)
}

extension Color {
    init(hex: UInt32) {
        self.init(red: Double((hex >> 16) & 0xFF) / 255,
                  green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255)
    }
}

/// Small, letter-spaced caps label: "TODAY", "NEEDS YOU".
struct Eyebrow: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View {
        Text(text.uppercased()).font(Theme.eyebrow).tracking(1.4).foregroundStyle(Theme.ink)
    }
}

/// A card in the stack: rounded top only, padded at the bottom so the next card
/// can tuck over it. Put several in `VStack(spacing: -Theme.overlap)`.
struct StackCard<Content: View>: View {
    let color: Color
    var index = 0
    @ViewBuilder let content: Content

    var body: some View {
        content
            .padding(.horizontal, 24)
            .padding(.top, 26)
            .padding(.bottom, 26 + Theme.overlap)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                UnevenRoundedRectangle(topLeadingRadius: Theme.cardRadius, topTrailingRadius: Theme.cardRadius,
                                       style: .continuous)
                    .fill(color)
                    .shadow(color: .black.opacity(0.06), radius: 12, y: -4)
            )
            .rise(index)
    }
}

/// Four-pointed sparkle, the app's motif. Concave sides via quad curves.
struct FourPointStar: Shape {
    var pinch: CGFloat = 0.18   // 0 = needle-thin points, 0.5 = diamond
    func path(in r: CGRect) -> Path {
        let c = CGPoint(x: r.midX, y: r.midY)
        let pts = [CGPoint(x: r.midX, y: r.minY), CGPoint(x: r.maxX, y: r.midY),
                   CGPoint(x: r.midX, y: r.maxY), CGPoint(x: r.minX, y: r.midY)]
        let k = pinch
        var p = Path()
        p.move(to: pts[0])
        for i in 0..<4 {
            let a = pts[i], b = pts[(i + 1) % 4]
            let ctrl = CGPoint(x: c.x + ((a.x + b.x) / 2 - c.x) * k * 2, y: c.y + ((a.y + b.y) / 2 - c.y) * k * 2)
            p.addQuadCurve(to: b, control: ctrl)
        }
        p.closeSubpath()
        return p
    }
}

extension View {
    /// Slide up into place, staggered by index: the stacked-cards entrance.
    func rise(_ index: Int) -> some View { modifier(Rise(index: index)) }
}

private struct Rise: ViewModifier {
    let index: Int
    @State private var shown = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    func body(content: Content) -> some View {
        content
            .offset(y: shown || reduceMotion ? 0 : 420)
            .opacity(shown ? 1 : 0)
            .onAppear {
                withAnimation(reduceMotion ? .easeOut(duration: 0.2)
                              : .spring(response: 0.7, dampingFraction: 0.82).delay(0.08 * Double(index))) { shown = true }
            }
    }
}

extension View {
    /// Cards sink back a touch as they scroll toward the edge: depth without decoration.
    func scrollDepth() -> some View {
        scrollTransition(.interactive, axis: .vertical) { content, phase in
            content
                .scaleEffect(phase.isIdentity ? 1 : 0.965, anchor: phase.value < 0 ? .bottom : .top)
                .opacity(phase.isIdentity ? 1 : 0.85)
        }
    }
}

/// Presses sink in slightly and tick the haptic: every tappable surface feels physical.
struct Squish: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.97 : 1)
            .brightness(configuration.isPressed ? -0.03 : 0)
            .animation(.spring(response: 0.25, dampingFraction: 0.7), value: configuration.isPressed)
            .sensoryFeedback(.impact(weight: .light), trigger: configuration.isPressed) { _, pressed in pressed }
    }
}

extension ButtonStyle where Self == Squish {
    static var squish: Squish { Squish() }
}

/// A status dot that breathes while something is live.
struct LiveDot: View {
    let color: Color
    var live = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    var body: some View {
        ZStack {
            if live && !reduceMotion {
                Circle().fill(color.opacity(0.35))
                    .phaseAnimator([false, true]) { c, big in
                        c.scaleEffect(big ? 2.4 : 1).opacity(big ? 0 : 1)
                    } animation: { _ in .easeOut(duration: 1.4) }
            }
            Circle().fill(color)
        }
        .frame(width: 8, height: 8)
    }
}

/// One-line message pinned to the top: what went wrong and what to do.
struct NoticeBanner: View {
    let text: String
    let dismiss: () -> Void
    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            FourPointStar().fill(Theme.action).frame(width: 16, height: 16).padding(.top, 2)
            Text(text).font(.subheadline).foregroundStyle(Theme.ink).frame(maxWidth: .infinity, alignment: .leading)
            Button(action: dismiss) { Image(systemName: "xmark").font(.caption.weight(.bold)) }
                .foregroundStyle(Theme.inkSoft)
                .accessibilityLabel("Dismiss")
        }
        .padding(16)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
        .shadow(color: .black.opacity(0.12), radius: 18, y: 8)
        .padding(.horizontal, 16)
    }
}

/// The app icon's sparkle: needle-thin rays (cubic sides that hug the axes).
struct Sparkle: Shape {
    var pinch: CGFloat = 0.14
    func path(in r: CGRect) -> Path {
        let c = CGPoint(x: r.midX, y: r.midY)
        let pts = [CGPoint(x: r.midX, y: r.minY), CGPoint(x: r.maxX, y: r.midY),
                   CGPoint(x: r.midX, y: r.maxY), CGPoint(x: r.minX, y: r.midY)]
        var p = Path()
        p.move(to: pts[0])
        for i in 0..<4 {
            let a = pts[i], b = pts[(i + 1) % 4]
            p.addCurve(to: b,
                       control1: CGPoint(x: c.x + (a.x - c.x) * pinch, y: c.y + (a.y - c.y) * pinch),
                       control2: CGPoint(x: c.x + (b.x - c.x) * pinch, y: c.y + (b.y - c.y) * pinch))
        }
        p.closeSubpath()
        return p
    }
}

/// Small sparkles that twinkle in turn around a hero star. Still when Reduce Motion is on.
struct Twinkles: View {
    var color: Color = .white
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    // (x, y, size) as fractions of the frame, staggered so only one or two shine at once.
    private let spots: [(CGFloat, CGFloat, CGFloat, Double)] = [
        (0.10, 0.16, 0.20, 0.0), (0.88, 0.22, 0.14, 0.6), (0.84, 0.86, 0.18, 1.1), (0.14, 0.82, 0.11, 1.6),
    ]

    var body: some View {
        GeometryReader { g in
            ForEach(Array(spots.enumerated()), id: \.offset) { _, s in
                let size = g.size.width * s.2
                Sparkle()
                    .fill(color)
                    .shadow(color: color.opacity(0.8), radius: size * 0.3)
                    .frame(width: size, height: size)
                    .position(x: g.size.width * s.0, y: g.size.height * s.1)
                    .modifier(Twinkle(delay: s.3, still: reduceMotion))
            }
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}

private struct Twinkle: ViewModifier {
    let delay: Double
    let still: Bool
    func body(content: Content) -> some View {
        if still {
            content.opacity(0.7)
        } else {
            content.phaseAnimator([false, true, false]) { view, on in
                view.scaleEffect(on ? 1 : 0.15).opacity(on ? 1 : 0).rotationEffect(.degrees(on ? 45 : 0))
            } animation: { on in
                on ? .easeOut(duration: 0.5).delay(delay) : .easeIn(duration: 0.7).delay(0.8)
            }
        }
    }
}

/// Warm light behind a star.
struct StarGlow: View {
    var color: Color = Theme.star
    var body: some View {
        Circle().fill(RadialGradient(colors: [color.opacity(0.75), color.opacity(0)], center: .center,
                                     startRadius: 0, endRadius: 60))
            .allowsHitTesting(false)
    }
}
