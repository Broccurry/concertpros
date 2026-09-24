
// Applied before first paint so switching themes never flashes the old one.
(function(){
  try{
    var t = localStorage.getItem("cp_theme");
    if(t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  }catch(e){}
})();
