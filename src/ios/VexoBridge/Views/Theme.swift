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
    // Each color has a dark twin: the same pastel family, deepened so cream ink reads on it.
    static let canvas    = Color(light: 0xE6E3D8, dark: 0x14130F)   // warm greige page / warm night
    static let card      = Color(light: 0xFFFFFF, dark: 0x25231E)
    static let butter    = Color(light: 0xF7E1AE, dark: 0x4A3D1E)
    static let mint      = Color(light: 0xC4E9A8, dark: 0x2C4423)
    static let rose      = Color(light: 0xF0AEC4, dark: 0x4D2836)
    static let sky       = Color(light: 0xEEF1FA, dark: 0x262B3B)
    static let pastels   = [sky, mint, rose, butter]

    // Ink + accents
    static let ink       = Color(light: 0x15130F, dark: 0xF6EFE2)
    static let inkSoft   = ink.opacity(0.6)
    static let inkFaint  = ink.opacity(0.32)
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

    /// Follows the app's light/dark appearance (Settings > Appearance).
    init(light: UInt32, dark: UInt32) {
        func ui(_ h: UInt32) -> UIColor {
            UIColor(red: CGFloat((h >> 16) & 0xFF) / 255, green: CGFloat((h >> 8) & 0xFF) / 255,
                    blue: CGFloat(h & 0xFF) / 255, alpha: 1)
        }
        self.init(uiColor: UIColor { $0.userInterfaceStyle == .dark ? ui(dark) : ui(light) })
    }
}

/// Settings > Appearance, stored as "system" | "light" | "dark".
enum Appearance {
    static func scheme(_ raw: String) -> ColorScheme? {
        raw == "light" ? .light : raw == "dark" ? .dark : nil
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

/// A card in the stack: inset from the screen edges and rounded on every corner, padded at the
/// bottom so the next card can tuck over it. Put several in `VStack(spacing: -Theme.overlap)`.
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
                RoundedRectangle(cornerRadius: Theme.cardRadius, style: .continuous)
                    .fill(color)
                    .shadow(color: .black.opacity(0.06), radius: 12, y: -4)
            )
            .rise(index)
    }
}

/// The app icon's star, the app's motif: four long needle rays plus four short diagonal ones.
/// No glow, no hot core: those belong to the icon only.
struct FourPointStar: Shape {
    func path(in r: CGRect) -> Path { LogoStar.long(in: r).union(LogoStar.short(in: r)) }
}

enum LogoStar {
    /// Proportions from scripts/render-icon.swift: long rays r, pinch 0.14; short rays 0.585 r at 45 degrees, pinch 0.10.
    static func long(in r: CGRect) -> Path { rays(in: r, scale: 1, pinch: 0.14, turn: 0) }
    static func short(in r: CGRect) -> Path { rays(in: r, scale: 0.585, pinch: 0.10, turn: .pi / 4) }

    private static func rays(in r: CGRect, scale: CGFloat, pinch k: CGFloat, turn: CGFloat) -> Path {
        let c = CGPoint(x: r.midX, y: r.midY)
        let rx = r.width / 2 * scale, ry = r.height / 2 * scale
        let pts = (0..<4).map { i -> CGPoint in
            let a = turn + CGFloat(i) * .pi / 2 - .pi / 2
            return CGPoint(x: c.x + cos(a) * rx, y: c.y + sin(a) * ry)
        }
        var p = Path()
        p.move(to: pts[0])
        for i in 0..<4 {
            let a = pts[i], b = pts[(i + 1) % 4]
            p.addCurve(to: b,
                       control1: CGPoint(x: c.x + (a.x - c.x) * k, y: c.y + (a.y - c.y) * k),
                       control2: CGPoint(x: c.x + (b.x - c.x) * k, y: c.y + (b.y - c.y) * k))
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

/// Same logo star, for the small twinkles.
typealias Sparkle = FourPointStar

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
