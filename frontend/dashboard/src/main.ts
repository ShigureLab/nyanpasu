import { createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { Auth } from './Auth';
import './style.css';

const root = document.getElementById('app');
if (!root) throw new Error('Missing #app');
createRoot(root).render(createElement(Auth));
