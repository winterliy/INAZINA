import { lazy } from "react";

// lazy() erases the generic parameter of LogCard<T>; restore the component type so rows/idOf keep their row type.
export const LogCard = lazy(() => import("./LogCard").then((module) => ({ default: module.LogCard }))) as unknown as typeof import("./LogCard").LogCard;
