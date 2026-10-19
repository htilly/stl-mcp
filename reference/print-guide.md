# Designing for FDM printing

Starting values for a 0.4 mm nozzle and 0.2 mm layers in PLA/PETG. Printers differ: when a fit
matters, suggest a small test piece first and say which value to change if it is too tight or loose.

## Walls and features
- Walls at least 1.2 mm (3 perimeters); 2 mm for enclosures and anything that takes screws or knocks.
- Floors and lids at least 1.2-1.6 mm (6-8 layers).
- Smallest printable detail about 0.4 mm wide; pins and posts under 2 mm diameter break easily.
- Embossed text at least 0.6 mm high and 5 mm tall (bold font); engraved text 0.4-0.6 mm deep.
- Bottom edges: a 0.4-0.5 mm chamfer hides the "elephant's foot" of the first layer.

## Clearances (gap per side unless noted)
- Press fit: 0.05-0.1 mm. Snug fit (lids that should hold): 0.15-0.2 mm.
- Sliding or rotating fit: 0.25-0.3 mm. Loose fit / parts printed in place: 0.4-0.5 mm.
- Friction-fit lids: lip 0.2 mm smaller per side than the opening, lip height 3-5 mm.
- PCB in a pocket: 0.5-1 mm around the board; boards and modules vary between makers.

## Holes and fasteners
- Vertical holes print 0.1-0.3 mm small: add 0.2 mm to the diameter (more for small holes).
- Horizontal holes sag at the top: use a teardrop or a flat top for holes over about 6 mm.
- Clearance holes: M2 2.4 mm, M2.5 2.9 mm, M3 3.4 mm, M4 4.5 mm, M5 5.5 mm.
- Self-tapping straight into plastic: M2 1.7 mm, M2.5 2.1 mm, M3 2.5-2.7 mm, at least 2 mm of wall around it.
- Heat-set inserts: follow the insert's datasheet; common M3 inserts (5.7 mm long) want a 4.0-4.2 mm
  hole about 6 mm deep, M2.5 3.5 mm, M2 3.2 mm, with at least 1.5-2 mm of wall around the hole.
- Socket cap heads (counterbore): M2 4 mm, M2.5 5 mm, M3 6 mm, M4 7.5 mm diameter; depth = head height + 0.2 mm.
- Nut traps (hex, across flats + 0.2-0.3 mm): M2 4.3 mm, M2.5 5.3 mm, M3 5.8 mm, M4 7.3 mm; depth = nut height + 0.2 mm.
- Printed threads are reliable from about M6 up; below that use inserts, nuts or self-tapping screws.

## Printing without support
- Overhangs up to 45 degrees from vertical print fine; steeper ones need a chamfer or support.
- Bridges up to about 15-20 mm are fine; longer spans sag.
- Put the largest flat face on the bed; split a part instead of supporting a big overhang.
- Ceilings of holes and slots in walls: use a 45 degree pointed top (teardrop/diamond).

## Snap fits and hinges
- Cantilever snap hooks: length at least 5-10 times the thickness, catch 0.5-1 mm deep; PETG
  tolerates more bending than PLA.
- Print-in-place hinges: 0.4-0.5 mm clearance between pin and knuckle.

## Enclosures for electronics
- Standoffs 3-5 mm high so pins and solder joints under the board clear the floor.
- Cut-outs for connectors 0.5-1 mm larger than the connector on every side; USB-C plugs need about
  13 x 7 mm for the overmould of most cables.
- Ventilation: slots 1.5-2 mm wide; keep them away from where fingers or screws go.
- Use lookup_part for board sizes and hole positions, or NopSCADlib in OpenSCAD for exact boards
  with connectors (pcb(RPI4), pcb_cutouts(RPI4)).
