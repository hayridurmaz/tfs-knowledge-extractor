import React from "react";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import OrderPage from "./pages/OrderPage";

export const App = React.memo(function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/orders">
          <Route path=":id" element={<OrderPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
});
