//
//  Star3D.swift
//  VexoBridge
//
//  The app's four-point star as a real 3D object: the same outline as
//  FourPointStar, extruded with chamfered edges in SceneKit and lit like a
//  glazed ceramic piece. Sways gently; a tap flicks it round once. Static when
//  Reduce Motion is on. No assets, no Blender: geometry is built at runtime.
//

import SwiftUI
import SceneKit

struct Star3D: UIViewRepresentable {
    var color: Color = Theme.star
    var spin = true
    /// 0...1: how thick the extrusion is relative to the star's width.
    var depth: CGFloat = 0.22
    /// Bump to tumble the star one full turn: up when it goes up, down when it goes down.
    var roll = 0

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func makeUIView(context: Context) -> SCNView {
        let view = SCNView()
        view.backgroundColor = .clear
        view.antialiasingMode = .multisampling4X
        view.isUserInteractionEnabled = true
        view.scene = makeScene()
        view.pointOfView = view.scene?.rootNode.childNode(withName: "camera", recursively: false)
        view.addGestureRecognizer(UITapGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.flick)))
        context.coordinator.view = view
        startIdle(view)
        return view
    }

    func updateUIView(_ view: SCNView, context: Context) {
        star(in: view)?.childNodes.first?.geometry?.firstMaterial?.diffuse.contents = UIColor(color)
        let c = context.coordinator
        guard roll != c.lastRoll else { return }
        defer { c.lastRoll = roll }
        guard !reduceMotion, let tilt = view.scene?.rootNode.childNode(withName: "tilt", recursively: true) else { return }
        // +x tips the top toward you (the face rolls down); -x rolls it up.
        let turn = SCNAction.rotateBy(x: roll > c.lastRoll ? .pi * 2 : -.pi * 2, y: 0, z: 0, duration: 0.8)
        turn.timingMode = .easeInEaseOut
        tilt.runAction(turn, forKey: "roll")
    }

    func makeCoordinator() -> Coordinator { Coordinator(reduceMotion: reduceMotion) }

    final class Coordinator: NSObject {
        weak var view: SCNView?
        var lastRoll = 0
        let reduceMotion: Bool
        init(reduceMotion: Bool) { self.reduceMotion = reduceMotion }

        @objc func flick() {
            guard !reduceMotion, let node = view?.scene?.rootNode.childNode(withName: "star", recursively: true) else { return }
            let spin = SCNAction.rotateBy(x: 0, y: .pi * 2, z: 0, duration: 0.9)
            spin.timingMode = .easeOut
            node.runAction(spin, forKey: "flick")
            UIImpactFeedbackGenerator(style: .soft).impactOccurred()
        }
    }

    // MARK: - Scene

    private func makeScene() -> SCNScene {
        let scene = SCNScene()
        scene.background.contents = UIColor.clear

        // Geometry: the logo star as two extrusions, long rays in front of the shorter, slimmer diagonals.
        let box = CGRect(x: -1, y: -1, width: 2, height: 2)
        func extrude(_ outline: Path, depth d: CGFloat) -> SCNShape {
            let bezier = UIBezierPath(cgPath: outline.cgPath)
            bezier.flatness = 0.004
            let shape = SCNShape(path: bezier, extrusionDepth: d * 2)
            shape.chamferRadius = d * 0.2
            shape.chamferMode = .both
            return shape
        }
        let longRays = extrude(LogoStar.long(in: box), depth: depth)
        let shortRays = extrude(LogoStar.short(in: box), depth: depth * 0.6)

        let material = SCNMaterial()
        material.lightingModel = .physicallyBased
        material.diffuse.contents = UIColor(color)
        material.roughness.contents = 0.32
        material.metalness.contents = 0.05
        material.clearCoat.contents = 0.6
        longRays.materials = [material]
        shortRays.materials = [material]

        let star = SCNNode()
        star.name = "star"
        star.addChildNode(SCNNode(geometry: longRays))
        star.addChildNode(SCNNode(geometry: shortRays))
        let tilt = SCNNode()
        tilt.name = "tilt"
        tilt.eulerAngles = SCNVector3(-0.14, 0.3, 0.06)   // resting pose: a little turned, so the depth reads
        tilt.addChildNode(star)
        scene.rootNode.addChildNode(tilt)

        // Light: soft key from top-left, warm fill, faint rim. Ambient keeps the shadow side from going muddy.
        func light(_ type: SCNLight.LightType, _ intensity: CGFloat, _ color: UIColor, at p: SCNVector3? = nil) {
            let l = SCNLight()
            l.type = type
            l.intensity = intensity
            l.color = color
            let n = SCNNode()
            n.light = l
            if let p { n.position = p; n.look(at: SCNVector3Zero) }
            scene.rootNode.addChildNode(n)
        }
        light(.ambient, 300, UIColor(white: 1, alpha: 1))
        light(.directional, 800, UIColor(white: 1, alpha: 1), at: SCNVector3(-3, 4, 5))
        light(.omni, 380, UIColor(red: 1, green: 0.82, blue: 0.7, alpha: 1), at: SCNVector3(3, -2, 3))
        light(.directional, 260, UIColor(white: 1, alpha: 1), at: SCNVector3(0, 2, -4))

        let camera = SCNNode()
        camera.name = "camera"
        camera.camera = SCNCamera()
        camera.camera?.fieldOfView = 30
        camera.position = SCNVector3(0, 0, 4.6)
        scene.rootNode.addChildNode(camera)
        return scene
    }

    private func star(in view: SCNView) -> SCNNode? {
        view.scene?.rootNode.childNode(withName: "star", recursively: true)
    }

    private func startIdle(_ view: SCNView) {
        guard spin, !reduceMotion, let node = star(in: view) else { return }
        // Sway, don't spin: a full turn shows the star edge-on, where it reads as a slab.
        let swayRight = SCNAction.rotateTo(x: 0.06, y: 0.55, z: 0, duration: 2.6, usesShortestUnitArc: true)
        let swayLeft = SCNAction.rotateTo(x: -0.06, y: -0.55, z: 0, duration: 2.6, usesShortestUnitArc: true)
        swayRight.timingMode = .easeInEaseOut
        swayLeft.timingMode = .easeInEaseOut
        let sway = SCNAction.repeatForever(.sequence([swayRight, swayLeft]))
        let bobUp = SCNAction.moveBy(x: 0, y: 0.06, z: 0, duration: 1.8)
        bobUp.timingMode = .easeInEaseOut
        let bob = SCNAction.repeatForever(.sequence([bobUp, bobUp.reversed()]))
        node.runAction(.group([sway, bob]), forKey: "idle")
    }
}
