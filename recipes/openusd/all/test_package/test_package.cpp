#include <pxr/usd/usd/stage.h>
#include <pxr/usd/usdGeom/mesh.h>
#include <pxr/usd/usdGeom/metrics.h>
#include <pxr/usd/usdGeom/xform.h>

PXR_NAMESPACE_USING_DIRECTIVE

int main() {
  UsdStageRefPtr stage = UsdStage::CreateNew("HelloWorld.usda");

  UsdGeomSetStageUpAxis(stage, UsdGeomTokens->y);
  UsdGeomSetStageMetersPerUnit(stage, 0.01);

  UsdGeomXform xform = UsdGeomXform::Define(stage, SdfPath("/root"));
  UsdGeomMesh mesh = UsdGeomMesh::Define(stage, SdfPath("/root/mesh"));
  stage->SetDefaultPrim(xform.GetPrim());

  stage->GetRootLayer()->Save();

  return EXIT_SUCCESS;
}
