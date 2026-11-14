//
//  Theme.swift
//  VexoBridge
//
//  Every color, radius, and type style the app uses. Placeholder palette
//  (warm canvas, pastel stacked cards, coral action) until Vexo's real theme
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
    func body(content: Content) -> some View {
        content
            .offset(y: shown ? 0 : 420)
            .opacity(shown ? 1 : 0)
            .onAppear {
                withAnimation(.spring(response: 0.7, dampingFraction: 0.82).delay(0.08 * Double(index))) { shown = true }
            }
    }
}
